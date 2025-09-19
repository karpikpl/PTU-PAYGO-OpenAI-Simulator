"""
Streamlit app: PTU (Provisioned Throughput Units) Calculator

Upload a CSV with the following columns (case-insensitive match, spaces allowed):
- timestamp [UTC]
- input tokens
- output tokens
- total tokens

The app analyzes token usage pat        # Display columns (matching original app + percentages)
        display_cols = [
            'num_ptus', 'ptu_capacity_tpm', 'ptu_input_tokens', 'ptu_output_tokens',
            'paygo_input_tokens', 'paygo_output_tokens', 'ptu_total_pct_formatted', 
            'ptu_monthly_cost_usd', 'paygo_monthly_cost_usd', 'total_monthly_cost_usd', 
            'utilization_pct_formatted'
        ]
        
        # Rename columns for display
        display_df = formatted_df[display_cols].copy()
        display_df.columns = [
            'PTUs', 'PTU Capacity (TPM)', 'PTU Input Tokens', 'PTU Output Tokens',
            'PAYGO Input Tokens', 'PAYGO Output Tokens', '% Tokens by PTU',
            'PTU Monthly Cost', 'PAYGO Monthly Cost', 'Total Monthly Cost', 'Utilization %'
        ]ares costs between:
1. PTU (Provisioned Throughput Units) pricing
2. PAYGO (Pay-as-you-go) pricing

Usage:
    pip install -r requirements.txt
    streamlit run app.py
"""

import streamlit as st
import pandas as pd

# Import our custom modules
from data_processing import prepare_dataframe, compute_minute_aggregation, compute_stats_per_date
from ptu_calculations import run_ptu_analysis, format_analysis_results
from pricing import load_pricing_data, get_price_ratio
from utils import create_download_link, get_dataset_duration_days, format_large_number


def main():
    """Main Streamlit application."""
    st.set_page_config(page_title="PTU Traffic Simulator", page_icon="📊", layout="wide")
    st.title("PTU vs PAYGO Traffic Simulator")
    st.markdown("Upload your OpenAI usage CSV to analyze PTU vs Pay-as-you-go pricing")

    # Sidebar for configuration
    st.sidebar.header("Configuration")
    
    # Load pricing data
    model_list, model_prices = load_pricing_data()
    
    if not model_list:
        st.sidebar.error("Could not load pricing data from openai_pricing.json")
        return
    
    # Model selection
    selected_model = st.sidebar.selectbox("Select Model", model_list, index=0)
    
    if selected_model not in model_prices:
        st.sidebar.error(f"Pricing data not found for {selected_model}")
        return
    
    input_price, output_price = model_prices[selected_model]
    
    # Display current pricing
    st.sidebar.write("**Current Pricing:**")
    st.sidebar.write(f"Input: ${input_price:.4f} per 1K tokens")
    st.sidebar.write(f"Output: ${output_price:.4f} per 1K tokens")
    
    # PTU Configuration
    st.subheader("PTU Pricing & Selection")
    
    # Default PTU pricing options
    default_ptu_prices = {
        'Monthly Reservation': 260.0,
        'Yearly Reservation': 221.0,
        'Hourly - Global ($1/Hour)': 730.0,
        'Hourly - Data Zone ($1.1/Hour)': 803.0,
        'Hourly - Regional ($2/Hour)': 1461.0,
        'Monthly Commitment (Deprecated)': 312.0,
    }

    # Two deployment preference selectors
    left, right = st.columns(2)
    with left:
        deployment = st.selectbox("Select PTU Regional Deployment Type Preference", options=["Global", "Regional"], index=0)
        st.caption(f"Deployment preference: {deployment}. Your selection affects minimum PTU deployment size and pricing used in comparisons.")
    with right:
        paygo_deployment = st.selectbox("Select PayGO Regional Deployment Type Preference", options=["Global", "Regional"], index=0)
        st.caption(f"PayGO deployment preference: {paygo_deployment}. This selection determines the PayGO pricing used in comparisons.")

    # Pricing option selector
    st.write("Choose a pricing scheme:")
    pricing_option = st.selectbox('Pricing option', options=list(default_ptu_prices.keys()), index=1)
    
    # Show pricing cards for reference
    opts = list(default_ptu_prices.items())
    for i in range(0, len(opts), 3):
        row = opts[i:i+3]
        cols = st.columns(len(row))
        for c, (name, price) in zip(cols, row):
            with c:
                st.markdown(f"**{name}**\n\n${price:.0f} USD/Month")

    # Prefill base price from selected option, allow manual override
    base_price_default = default_ptu_prices.get(pricing_option, 221.0)
    base_ptu_monthly = st.number_input("Base PTU Monthly Price (USD)", value=float(base_price_default), min_value=0.0, format="%.2f")

    # Discount and final price
    discount_pct = st.number_input("PTU Discount Percentage (%)", value=0.0, min_value=0.0, max_value=100.0, step=0.1, help="Enter e.g. 14.5 for 14.5% discount")
    final_ptu_price = round(base_ptu_monthly * (1 - discount_pct / 100.0), 2)
    
    # Show both base and final price side-by-side
    p1, p2 = st.columns(2)
    p1.metric("Base PTU Monthly Price (USD)", f"${base_ptu_monthly:.2f}")
    p2.metric("Final PTU Monthly Price (USD)", f"${final_ptu_price:.2f}")

    # PTU capacity per unit (default for GPT-4.1)
    st.subheader("PTU Capacity Configuration")
    default_tpm = 3000 if "gpt-4" in selected_model.lower() else 1000
    ptu_capacity_tpm = st.number_input(
        "PTU Capacity (TPM per unit)", 
        min_value=100, 
        max_value=50000, 
        value=default_tpm,
        help="Tokens per minute capacity per PTU unit"
    )
    
    # PTU analysis range
    st.subheader("Analysis Range")
    col1, col2 = st.columns(2)
    with col1:
        min_ptu_count = st.number_input("Min PTU Count", min_value=15, max_value=1000, value=15, step=5)
    with col2:
        max_ptu_count = st.number_input("Max PTU Count", min_value=15, max_value=1000, value=100, step=5)
    
    if min_ptu_count > max_ptu_count:
        st.error("Min PTU count must be <= Max PTU count")
        return
    
    # File upload
    st.header("Upload Usage Data")
    uploaded_file = st.file_uploader(
        "Choose CSV file", 
        type="csv",
        help="Upload CSV with columns: timestamp, input tokens, output tokens"
    )
    
    if uploaded_file is None:
        st.info("Please upload a CSV file to begin analysis. Columns required: timestamp [UTC] format: 8/18/2025, 12:00:38.941 AM,input_tokens,output_tokens,total_tokens")
        return
    
    # Process uploaded file (with caching to avoid re-reading)
    @st.cache_data
    def process_uploaded_file(file_content, file_name):
        """Cache file processing to avoid re-reading on button clicks"""
        try:
            # Convert bytes to string for CSV reading
            import io
            string_data = io.StringIO(file_content.decode('utf-8'))
            df = pd.read_csv(string_data, dtype=str)
            processed_df, error_msg = prepare_dataframe(df)
            return processed_df, error_msg
        except Exception as e:
            return None, str(e)
    
    # Get file content for caching (file object changes on each run)
    file_content = uploaded_file.getvalue()
    file_name = uploaded_file.name
    
    with st.spinner("Processing uploaded file..."):
        processed_df, error_msg = process_uploaded_file(file_content, file_name)
        
        if error_msg:
            st.error(f"Error processing file: {error_msg}")
            return
    
    st.success(f"✅ Processed {len(processed_df):,} requests")
    
    # Calculate dataset metrics
    dataset_days = get_dataset_duration_days(processed_df, 'timestamp')
    
    # Compute minute-level aggregations
    minute_series = compute_minute_aggregation(processed_df)
    
    if minute_series.empty:
        st.error("No data available after aggregation")
        return
    
    # Display basic statistics (matching original app layout)
    st.header("Dataset Overview")
    
    # Show token distribution and peak metrics (3 columns like original)
    total_input = minute_series['input_tokens'].sum()
    total_output = minute_series['output_tokens'].sum()
    peak_tpm = minute_series['tokens_per_minute'].max()
    
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Peak TPM", f"{peak_tpm:,.0f}", help="Peak tokens per minute in dataset")
    with col2:
        st.metric("Total input tokens", f"{total_input:,}")
    with col3:
        st.metric("Total output tokens", f"{total_output:,}")
        
    # Additional metrics below
    col1, col2, col3 = st.columns(3)
    with col1:
        total_requests = len(processed_df)
        st.metric("Total Requests", f"{total_requests:,}")
    with col2:
        st.metric("Dataset Duration", f"{dataset_days:.1f} days")
    with col3:
        avg_tpm = minute_series['tokens_per_minute'].mean()
        st.metric("Average TPM", format_large_number(avg_tpm))
    
    # PTU Analysis
    st.header("PTU Cost Analysis")
    
    if st.button("Run PTU Analysis", type="primary"):
        
        # Calculate output weight for PTU capacity
        output_weight = get_price_ratio(input_price, output_price)
        
        # Create progress tracking
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        def update_progress(progress):
            progress_bar.progress(progress)
        
        def update_status(status):
            status_text.text(status)
        
        with st.spinner("Running PTU simulation..."):
            # Run the analysis
            results_df = run_ptu_analysis(
                request_data=processed_df,
                minute_series=minute_series,
                min_ptu_count=min_ptu_count,
                max_ptu_count=max_ptu_count,
                ptu_capacity_tpm=ptu_capacity_tpm,
                final_ptu_price=final_ptu_price,
                input_price=input_price,
                output_price=output_price,
                dataset_days=dataset_days,
                output_weight=output_weight,
                progress_callback=update_progress,
                status_callback=update_status
            )
        
        # Format results for display
        formatted_df = format_analysis_results(results_df)
        
        st.subheader('PTU Analysis Results')
        
        # Display results table with formatted numbers
        display_cols = [
            'num_ptus', 'ptu_capacity_tpm_formatted', 'ptu_input_tokens_formatted', 'ptu_output_tokens_formatted',
            'paygo_input_tokens_formatted', 'paygo_output_tokens_formatted', 'ptu_total_pct_formatted', 
            'ptu_monthly_cost_usd', 'paygo_monthly_cost_usd', 'total_monthly_cost_usd', 
            'utilization_pct_formatted'
        ]
        
        # Rename columns for display
        display_df = formatted_df[display_cols].copy()
        display_df.columns = [
            'PTUs', 'PTU Capacity (TPM)', 'PTU Input Tokens', 'PTU Output Tokens',
            'PAYGO Input Tokens', 'PAYGO Output Tokens', '% Tokens by PTU',
            'PTU Monthly Cost', 'PAYGO Monthly Cost', 'Total Monthly Cost', 'Utilization %'
        ]
        
        st.dataframe(display_df, height=400, use_container_width=True)
        
        # Charts
        st.subheader('Cost Analysis Charts')
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.write("**Monthly Costs by PTU Count**")
            cost_chart_df = results_df.set_index('num_ptus')[['ptu_monthly_cost', 'paygo_monthly_cost']]
            cost_chart_df.columns = ['PTU Cost', 'PAYGO Cost']
            st.line_chart(cost_chart_df)
        
        with col2:
            st.write("**Token Distribution by PTU Count**")
            token_chart_df = results_df.set_index('num_ptus')[
                ['ptu_input_tokens', 'ptu_output_tokens', 'paygo_input_tokens', 'paygo_output_tokens']
            ]
            token_chart_df.columns = ['PTU Input', 'PTU Output', 'PAYGO Input', 'PAYGO Output']
            st.bar_chart(token_chart_df)
        
        # Traffic optimization insights
        st.subheader("Traffic Optimization")
        
        # Find PTU configuration closest to PAYGO cost (likely more expensive)
        paygo_only_cost = results_df[results_df['num_ptus'] == 0]['total_monthly_cost'].iloc[0]
        ptu_configs = results_df[results_df['num_ptus'] > 0].copy()
        
        if not ptu_configs.empty:
            # Find configuration with cost closest to PAYGO (above PAYGO cost)
            ptu_configs['cost_diff'] = ptu_configs['total_monthly_cost'] - paygo_only_cost
            # Prefer configurations slightly above PAYGO cost
            above_paygo = ptu_configs[ptu_configs['cost_diff'] >= 0]
            if not above_paygo.empty:
                closest_idx = above_paygo['cost_diff'].idxmin()
            else:
                # If all are below PAYGO, take the one closest to PAYGO
                closest_idx = ptu_configs['cost_diff'].abs().idxmin()
            
            optimal_config = results_df.loc[closest_idx]
            
            col1, col2, col3 = st.columns(3)
            
            with col1:
                st.metric(
                    "Recommended PTU Count", 
                    f"{optimal_config['num_ptus']:.0f}",
                    help="PTU configuration closest to PAYGO cost for traffic optimization"
                )
            
            with col2:
                # Get percentage of tokens handled by PTU from formatted results
                optimal_formatted = formatted_df.loc[closest_idx]
                tokens_optimized_pct = optimal_formatted['ptu_total_pct']
                st.metric(
                    "% of Optimized Tokens", 
                    f"{tokens_optimized_pct:.1f}%",
                    help="Percentage of tokens handled by PTU vs PAYGO"
                )
            
            with col3:
                cost_diff_pct = ((optimal_config['total_monthly_cost'] - paygo_only_cost) / paygo_only_cost * 100)
                is_more_expensive = optimal_config['total_monthly_cost'] > paygo_only_cost
                
                # Choose badge color: orange if more expensive, green if cheaper
                badge_color = "#ff8c00" if is_more_expensive else "#28a745"  # Orange or Green
                badge_text = f"{cost_diff_pct:+.1f}% vs PAYGO"
                
                st.markdown(f"""
                <div style="text-align: left;">
                    <p style="font-size: 14px; color: #8e8ea0; margin-bottom: 4px;">Total Monthly Cost</p>
                    <p style="font-size: 28px; font-weight: 600; margin-bottom: 4px;">${optimal_config['total_monthly_cost']:,.2f}</p>
                    <span style="background-color: {badge_color}; color: white; padding: 2px 8px; border-radius: 12px; font-size: 12px; font-weight: 500;">
                        {badge_text}
                    </span>
                </div>
                """, unsafe_allow_html=True)
        
        # Download link
        st.markdown(
            create_download_link(
                formatted_df, 
                'ptu_analysis_results.csv', 
                '📥 Download Analysis Results (CSV)'
            ), 
            unsafe_allow_html=True
        )


if __name__ == "__main__":
    main()
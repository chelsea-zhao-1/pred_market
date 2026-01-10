import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import os

def main():
    # File paths
    script_dir = os.path.dirname(os.path.abspath(__file__))
    kalshi_path = os.path.join(script_dir, 'kalshi_giants_vs_raiders_ingame_prices.csv')
    polymarket_path = os.path.join(script_dir, 'polymarket_giants_vs_raiders_ingame_prices.csv')

    # Load data
    print("Loading data...")
    df_kalshi = pd.read_csv(kalshi_path)
    df_poly = pd.read_csv(polymarket_path)

    # Convert datetime columns to datetime objects
    df_kalshi['datetime_est'] = pd.to_datetime(df_kalshi['datetime_est'])
    df_poly['datetime_est'] = pd.to_datetime(df_poly['datetime_est'])

    # Set index to datetime
    df_kalshi.set_index('datetime_est', inplace=True)
    df_poly.set_index('datetime_est', inplace=True)

    # Resample to 1-minute intervals to align data
    # Taking the last price in each minute bucket and forward filling missing values
    df_kalshi_resampled = df_kalshi.resample('1min').last().ffill()
    df_poly_resampled = df_poly.resample('1min').last().ffill()

    # Align the two dataframes
    # We want the intersection of times where we have data for both, or the union?
    # Let's align on the union of their indices to see the full picture, filling forward.
    combined_df = pd.merge(
        df_kalshi_resampled[['price_giants']], 
        df_poly_resampled[['price_raiders']], 
        left_index=True, 
        right_index=True, 
        how='inner', # Keep only times where we have info for both, or use outer/ffill
        suffixes=('_kalshi', '_poly')
    )
    
    # Rename columns for clarity
    combined_df.rename(columns={
        'price_giants': 'Kalshi Giants', 
        'price_raiders': 'Polymarket Raiders'
    }, inplace=True)

    print(f"Data alignment complete. {len(combined_df)} records.")

    if combined_df.empty:
        print("No overlapping data found between the two datasets.")
        return

    # Plotting
    print("Creating plot...")
    fig, ax = plt.subplots(figsize=(12, 6))

    # Create stacked bar chart
    # Since we have time series, bar width needs to be set appropriately. 
    # With many points, simple .plot(kind='bar', stacked=True) puts categorical labels on x-axis which is messy.
    # Using matplotlib directly for better time axis control.

    # Calculate width of bars (e.g. 0.8 minutes in days)
    width = 0.8 / (24 * 60) 

    dates = combined_df.index
    kalshi_prices = combined_df['Kalshi Giants']
    poly_prices = combined_df['Polymarket Raiders']

    # Plot bars
    ax.bar(dates, kalshi_prices, width=width, label='Kalshi Giants', align='center', alpha=0.8)
    ax.bar(dates, poly_prices, width=width, bottom=kalshi_prices, label='Polymarket Raiders', align='center', alpha=0.8)

    # Formatting x-axis
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    ax.xaxis.set_major_locator(mdates.MinuteLocator(interval=15)) # Show label every 15 mins
    plt.xticks(rotation=45)

    # Add a horizontal line at 1.0 (Sum = 100%)
    ax.axhline(y=1.0, color='r', linestyle='--', label='Price Sum = 1.0')

    # Labels and Title
    ax.set_ylabel('Price ($)')
    ax.set_xlabel('Time (EST)')
    ax.set_title('Stacked Prices: Kalshi (Giants) + Polymarket (Raiders)')
    ax.legend()

    # Layout adjustment
    plt.tight_layout()

    # Save plot
    output_file = 'stacked_prices_chart.png'
    plt.savefig(output_file)
    print(f"Plot saved to {output_file}")
    
    # Show plot (optional, might not work in headless env, but good for local)
    # plt.show()

if __name__ == "__main__":
    main()

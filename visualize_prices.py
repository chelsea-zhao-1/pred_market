import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# Load the data
poly_file = 'polymarket_giants_vs_raiders_ingame_prices.csv'
kalshi_file = 'kalshi_giants_vs_raiders_ingame_prices.csv'

try:
    df_poly = pd.read_csv(poly_file)
    df_kalshi = pd.read_csv(kalshi_file)

    # Convert datetime columns
    df_poly['datetime_est'] = pd.to_datetime(df_poly['datetime_est'])
    df_kalshi['datetime_est'] = pd.to_datetime(df_kalshi['datetime_est'])

    # Create the plot
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 10), sharex=True)

    # Plot Giants Prices
    ax1.plot(df_poly['datetime_est'], df_poly['price_giants'], label='Polymarket', color='blue', linewidth=2)
    ax1.plot(df_kalshi['datetime_est'], df_kalshi['price_giants'], label='Kalshi', color='cyan', linewidth=2)
    ax1.set_title('Giants Win Probability: Polymarket vs Kalshi')
    ax1.set_ylabel('Implied Probability')
    ax1.legend()
    ax1.grid(True, alpha=0.3)

    # Plot Raiders Prices
    ax2.plot(df_poly['datetime_est'], df_poly['price_raiders'], label='Polymarket', color='red', linewidth=2)
    ax2.plot(df_kalshi['datetime_est'], df_kalshi['price_raiders'], label='Kalshi', color='orange', linewidth=2)
    ax2.set_title('Raiders Win Probability: Polymarket vs Kalshi')
    ax2.set_ylabel('Implied Probability')
    ax2.set_xlabel('Time (EST)')
    ax2.legend()
    ax2.grid(True, alpha=0.3)

    # Format x-axis dates
    ax2.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M', tz=df_poly['datetime_est'].dt.tz))
    plt.xticks(rotation=45)

    plt.tight_layout()
    
    # Save the plot
    output_file = 'price_comparison_plot.png'
    plt.savefig(output_file)
    print(f"Plot saved to '{output_file}'")

except Exception as e:
    print(f"An error occurred: {e}")

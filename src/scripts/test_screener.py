import pandas as pd
from src.screener import MasterScreener

def run_granular_test():
    # Point the orchestrator to the mock database we just built
    screener = MasterScreener(db_path="test_history.db")
    
    test_universe = [
        {"token": "1001", "symbol": "BULL-EQ"},
        {"token": "1002", "symbol": "BEAR-EQ"},
        {"token": "1003", "symbol": "CHOP-EQ"}
    ]
    
    print("Ingesting mock data and calculating modular scores...")
    results = []
    
    for stock in test_universe:
        df = screener.fetch_data(stock['token'])
        
        if len(df) < 252:
            print(f"Skipping {stock['symbol']}: Insufficient data.")
            continue
            
        # We bypass the MasterScreener's evaluate_stock() method here 
        # to forcefully extract the individual module scores for the CSV.
        row_data = {
            "Symbol": stock['symbol'],
            "Master_Score": 0.0
        }
        
        for category, scorer_list in screener.scorers.items():
            category_sum = 0.0
            
            for scorer in scorer_list:
                # Extract the class name dynamically (e.g., "HistoricalVolatilityScorer")
                module_name = scorer.__class__.__name__
                
                # Calculate the individual attribute score
                score = scorer.calculate(df)
                row_data[module_name] = round(score, 4)
                category_sum += score
                
            # Store the category average
            avg_cat_score = category_sum / len(scorer_list) if scorer_list else 0.0
            row_data[f"CAT_{category.upper()}"] = round(avg_cat_score, 4)
            
            # Add to Master Score via weights
            row_data["Master_Score"] += avg_cat_score * screener.category_weights[category]
            
        row_data["Master_Score"] = round(row_data["Master_Score"], 4)
        results.append(row_data)

    # Convert to DataFrame, sort by highest master score, and export
    results_df = pd.DataFrame(results)
    results_df = results_df.sort_values(by="Master_Score", ascending=False)
    
    csv_filename = "mock_screener_results.csv"
    results_df.to_csv(csv_filename, index=False)
    
    print(f"\nTest complete. Results exported to {csv_filename}")
    print("\nPreview of Master Scores:")
    print(results_df[['Symbol', 'Master_Score', 'CAT_MOMENTUM', 'CAT_VOLATILITY']])

if __name__ == "__main__":
    run_granular_test()
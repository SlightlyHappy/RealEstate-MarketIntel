"""
Brokerage Intelligence System - Property Price Prediction & Market Analysis
Designed for brokerage websites to show:
- Price estimator (what to list)
- Market heatmap (location rankings)
- Deal finder (over/underpriced detection)
"""

import json
import pandas as pd
import numpy as np
from pathlib import Path
import pickle
from sklearn.model_selection import train_test_split, cross_val_score
from sklearn.preprocessing import LabelEncoder
from sklearn.linear_model import LinearRegression
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
import warnings
warnings.filterwarnings('ignore')

# ============================================================================
# PART 1: DATA LOADING & CLEANING
# ============================================================================

def load_data():
    """Load JSONL data from scraper output"""
    data_file = Path("data/raw/magicbricks_all_cities.jsonl")
    
    records = []
    with open(data_file, 'r', encoding='utf-8') as f:
        for line in f:
            if line.strip():
                records.append(json.loads(line))
    
    df = pd.DataFrame(records)
    return df


def normalize_prices(df):
    """Normalize all prices to Crores"""
    def to_crore(row):
        price = float(row['price'])
        if row['price_unit'] == 'Lac':
            return price / 100
        return price
    
    df['price_cr'] = df.apply(to_crore, axis=1)
    return df


def clean_data(df):
    """Remove anomalies and prepare for modeling"""
    
    print(f"Original records: {len(df)}")
    
    # Convert dtypes
    df['bhk'] = pd.to_numeric(df['bhk'], errors='coerce')
    df['area_sqft'] = pd.to_numeric(df['area_sqft'], errors='coerce')
    
    # Remove invalid BHK values (1-5 BHK only - realistic for Indian market)
    df = df[(df['bhk'] >= 1) & (df['bhk'] <= 5)].copy()
    
    # Remove unrealistic areas
    df = df[(df['area_sqft'] >= 300) & (df['area_sqft'] <= 10000)].copy()
    
    # Remove unrealistic prices (0.5 Cr to 50 Cr range for residential)
    df = df[(df['price_cr'] >= 0.3) & (df['price_cr'] <= 50)].copy()
    
    # Remove properties without complete data
    df = df.dropna(subset=['bhk', 'area_sqft', 'price_cr', 'location'])
    
    print(f"After cleaning: {len(df)}")
    
    return df


def feature_engineering(df):
    """Create features for modeling"""
    
    # Price per sqft (market value indicator)
    df['price_per_sqft'] = (df['price_cr'] * 10_000_000) / df['area_sqft']
    
    # Location grouping (consolidate minor locations)
    location_counts = df['location'].value_counts()
    major_locations = location_counts[location_counts >= 20].index.tolist()
    df['location_grouped'] = df['location'].apply(
        lambda x: x if x in major_locations else 'Other'
    )
    
    # Encode categorical variables
    le_location = LabelEncoder()
    le_ptype = LabelEncoder()
    
    df['location_encoded'] = le_location.fit_transform(df['location_grouped'])
    df['ptype_encoded'] = le_ptype.fit_transform(df['property_type'])
    
    return df, le_location, le_ptype


# ============================================================================
# PART 2: MODEL TRAINING
# ============================================================================

def train_models(X_train, y_train, X_test, y_test):
    """Train baseline and ensemble models"""
    
    models = {}
    results = {}
    
    # Model 1: Linear Regression (baseline + interpretability)
    print("\nTraining Linear Regression...")
    lr = LinearRegression()
    lr.fit(X_train, y_train)
    y_pred_lr = lr.predict(X_test)
    
    models['linear_regression'] = lr
    results['linear_regression'] = {
        'mae': mean_absolute_error(y_test, y_pred_lr),
        'rmse': np.sqrt(mean_squared_error(y_test, y_pred_lr)),
        'r2': r2_score(y_test, y_pred_lr),
        'predictions': y_pred_lr
    }
    
    # Model 2: Random Forest (high accuracy, handles non-linearity)
    print("Training Random Forest...")
    rf = RandomForestRegressor(n_estimators=100, max_depth=20, random_state=42, n_jobs=-1)
    rf.fit(X_train, y_train)
    y_pred_rf = rf.predict(X_test)
    
    models['random_forest'] = rf
    results['random_forest'] = {
        'mae': mean_absolute_error(y_test, y_pred_rf),
        'rmse': np.sqrt(mean_squared_error(y_test, y_pred_rf)),
        'r2': r2_score(y_test, y_pred_rf),
        'predictions': y_pred_rf,
        'feature_importance': rf.feature_importances_
    }
    
    return models, results


def print_model_comparison(results, X_test_cols):
    """Print model performance"""
    print("\n" + "="*70)
    print("MODEL PERFORMANCE COMPARISON")
    print("="*70)
    
    for model_name, metrics in results.items():
        print(f"\n{model_name.upper().replace('_', ' ')}:")
        print(f"  MAE (Mean Absolute Error): {metrics['mae']:.2f} Cr")
        print(f"  RMSE (Root Mean Sq Error): {metrics['rmse']:.2f} Cr")
        print(f"  R² Score: {metrics['r2']:.4f}")
    
    # Feature importance for Random Forest
    if 'random_forest' in results:
        print(f"\nRANDOM FOREST - TOP FEATURES:")
        importance = results['random_forest']['feature_importance']
        features = X_test_cols
        top_features = sorted(zip(features, importance), key=lambda x: x[1], reverse=True)[:5]
        for feat, imp in top_features:
            print(f"  {feat}: {imp:.4f}")


# ============================================================================
# PART 3: MARKET INTELLIGENCE
# ============================================================================

def generate_market_heatmap(df, model):
    """Generate location-based market intelligence"""
    
    print("\n" + "="*70)
    print("MARKET HEATMAP - LOCATION INTELLIGENCE")
    print("="*70)
    
    location_stats = []
    
    for location in df['location_grouped'].unique():
        loc_data = df[df['location_grouped'] == location]
        
        avg_price = loc_data['price_cr'].mean()
        avg_price_sqft = loc_data['price_per_sqft'].mean()
        median_area = loc_data['area_sqft'].median()
        count = len(loc_data)
        
        location_stats.append({
            'location': location,
            'avg_price_cr': avg_price,
            'avg_price_per_sqft': avg_price_sqft,
            'median_area_sqft': median_area,
            'count': count,
            'hotness': 'HOT' if count > 100 else 'WARM' if count > 50 else 'COOL'
        })
    
    heatmap_df = pd.DataFrame(location_stats).sort_values('avg_price_per_sqft', ascending=False)
    
    print("\n{:<20} {:<15} {:<20} {:<12} {:<8}".format(
        "Location", "Avg Price (Cr)", "Price/sqft", "Props", "Status"
    ))
    print("-" * 80)
    
    for _, row in heatmap_df.iterrows():
        print("{:<20} {:<15.2f} {:<20.0f} {:<12} {:<8}".format(
            row['location'][:20], row['avg_price_cr'], 
            row['avg_price_per_sqft'], row['count'], row['hotness']
        ))
    
    return heatmap_df


def find_deals(df, model, le_location, le_ptype, feature_cols):
    """Find over/underpriced properties"""
    
    print("\n" + "="*70)
    print("DEAL FINDER - PRICE ANOMALIES")
    print("="*70)
    
    # Prepare features for prediction
    X_all = df[feature_cols].values
    
    # Get predictions from Random Forest (most accurate)
    predicted_prices = model.predict(X_all)
    
    # Calculate residuals (actual - predicted)
    df['predicted_price'] = predicted_prices
    df['price_deviation_pct'] = ((df['price_cr'] - df['predicted_price']) / df['predicted_price'] * 100)
    
    # Find deals
    underpriced = df[df['price_deviation_pct'] < -15].nlargest(5, 'area_sqft')  # 15% below market
    overpriced = df[df['price_deviation_pct'] > 15].nlargest(5, 'area_sqft')   # 15% above market
    
    print("\nBEST DEALS (Underpriced by 15%+):")
    print("-" * 80)
    for _, prop in underpriced.iterrows():
        print(f"  {prop['location']}: {prop['bhk']:.0f} BHK, {prop['area_sqft']:.0f} sqft")
        print(f"    Listed: {prop['price_cr']:.2f} Cr | Fair: {prop['predicted_price']:.2f} Cr")
        print(f"    SAVE: {abs(prop['price_deviation_pct']):.1f}% ({abs((prop['price_cr']-prop['predicted_price'])*10_000_000):.0f} Rs)")
    
    print("\nOVERPRICED (Above market by 15%+):")
    print("-" * 80)
    for _, prop in overpriced.iterrows():
        print(f"  {prop['location']}: {prop['bhk']:.0f} BHK, {prop['area_sqft']:.0f} sqft")
        print(f"    Listed: {prop['price_cr']:.2f} Cr | Fair: {prop['predicted_price']:.2f} Cr")
        print(f"    OVERPRICE: {prop['price_deviation_pct']:.1f}% ({(prop['price_cr']-prop['predicted_price'])*10_000_000:.0f} Rs)")
    
    return df


def price_estimator_example(model, le_location, le_ptype, feature_cols):
    """Demo: Estimate price for a property"""
    
    print("\n" + "="*70)
    print("PRICE ESTIMATOR - USAGE EXAMPLE")
    print("="*70)
    
    # Example: 3 BHK, 1500 sqft in Mumbai
    bhk = 3
    area = 1500
    location = 'Mumbai'
    ptype = 'Apartment'
    
    # Encode
    loc_encoded = le_location.transform([location])[0]
    ptype_encoded = le_ptype.transform([ptype])[0]
    price_per_sqft = 0  # Will be computed by model
    
    # Create feature vector
    X_sample = np.array([[bhk, area, loc_encoded, ptype_encoded, price_per_sqft]])
    
    # Predict
    try:
        predicted = model.predict(X_sample)[0]
        print(f"\nProperty: {bhk} BHK, {area} sqft, {location}, {ptype}")
        print(f"Estimated Price: {predicted:.2f} Cr")
        print(f"Price per sqft: {(predicted * 10_000_000 / area):,.0f} Rs")
    except:
        print("Note: Model needs feature adjustment for direct use")


# ============================================================================
# MAIN EXECUTION
# ============================================================================

def main():
    print("="*70)
    print("BROKERAGE INTELLIGENCE SYSTEM")
    print("="*70)
    
    # Load and clean data
    print("\n1. LOADING DATA...")
    df = load_data()
    df = normalize_prices(df)
    df = clean_data(df)
    
    # Feature engineering
    print("\n2. FEATURE ENGINEERING...")
    df, le_location, le_ptype = feature_engineering(df)
    
    print(f"Processed records: {len(df)}")
    print(f"Major locations: {df['location_grouped'].nunique()}")
    
    # Prepare training data
    feature_cols = ['bhk', 'area_sqft', 'location_encoded', 'ptype_encoded', 'price_per_sqft']
    X = df[feature_cols].values
    y = df['price_cr'].values
    
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42
    )
    
    # Train models
    print("\n3. TRAINING MODELS...")
    models, results = train_models(X_train, y_train, X_test, y_test)
    
    # Show results
    print_model_comparison(results, feature_cols)
    
    # Market intelligence
    print("\n4. GENERATING MARKET INTELLIGENCE...")
    heatmap = generate_market_heatmap(df, models['random_forest'])
    
    deals = find_deals(df, models['random_forest'], le_location, le_ptype, feature_cols)
    
    # Price estimator demo
    price_estimator_example(models['random_forest'], le_location, le_ptype, feature_cols)
    
    # Save models
    print("\n5. SAVING MODELS...")
    with open('models/price_predictor_rf.pkl', 'wb') as f:
        pickle.dump(models['random_forest'], f)
    with open('models/encoders.pkl', 'wb') as f:
        pickle.dump((le_location, le_ptype), f)
    
    print("Models saved to models/")
    print("\n" + "="*70)
    print("SYSTEM READY FOR DEPLOYMENT")
    print("="*70)


if __name__ == "__main__":
    # Create models directory
    Path("models").mkdir(exist_ok=True)
    main()

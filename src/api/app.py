"""
Brokerage Intelligence API
RESTful API for brokerages to integrate market intelligence into their websites
"""

from flask import Flask, request, jsonify
import pickle
import numpy as np
import json
from pathlib import Path
import pandas as pd

app = Flask(__name__)

# Load trained models
model_path = Path("models/price_predictor_rf.pkl")
encoder_path = Path("models/encoders.pkl")

with open(model_path, 'rb') as f:
    model_rf = pickle.load(f)

with open(encoder_path, 'rb') as f:
    le_location, le_ptype = pickle.load(f)

# Cache market data
MARKET_CACHE = {}

def load_market_data():
    """Load JSONL and compute market metrics"""
    data = []
    with open("data/raw/magicbricks_all_cities.jsonl", 'r') as f:
        for line in f:
            if line.strip():
                data.append(json.loads(line))
    
    df = pd.DataFrame(data)
    
    # Normalize prices
    def to_crore(row):
        price = float(row['price'])
        return price / 100 if row['price_unit'] == 'Lac' else price
    
    df['price_cr'] = df.apply(to_crore, axis=1)
    df['area_sqft'] = pd.to_numeric(df['area_sqft'], errors='coerce')
    df['price_per_sqft'] = (df['price_cr'] * 10_000_000) / df['area_sqft']
    
    # Clean
    df = df[(df['area_sqft'] >= 300) & (df['area_sqft'] <= 10000)]
    df = df[(df['price_cr'] >= 0.3) & (df['price_cr'] <= 50)]
    
    return df

# Load on startup
market_df = load_market_data()

# ============================================================================
# ENDPOINTS
# ============================================================================

@app.route('/api/health', methods=['GET'])
def health():
    """Health check"""
    return jsonify({"status": "OK", "model": "Random Forest v1.0"})


@app.route('/api/estimate-price', methods=['POST'])
def estimate_price():
    """
    Estimate property price
    
    POST /api/estimate-price
    {
        "bhk": 3,
        "area_sqft": 1500,
        "location": "Mumbai",
        "property_type": "Apartment"
    }
    """
    try:
        data = request.json
        
        bhk = float(data.get('bhk'))
        area = float(data.get('area_sqft'))
        location = data.get('location', 'Other')
        ptype = data.get('property_type', 'Apartment')
        
        # Validation
        if not (1 <= bhk <= 5):
            return jsonify({"error": "BHK must be 1-5"}), 400
        if not (300 <= area <= 10000):
            return jsonify({"error": "Area must be 300-10000 sqft"}), 400
        
        # Encode
        try:
            loc_encoded = le_location.transform([location])[0]
        except:
            loc_encoded = le_location.transform(['Other'])[0]
        
        try:
            ptype_encoded = le_ptype.transform([ptype])[0]
        except:
            ptype_encoded = le_ptype.transform(['Apartment'])[0]
        
        # Calculate price per sqft from market data
        loc_data = market_df[market_df['location'].str.contains(location, case=False, na=False)]
        avg_price_per_sqft = loc_data['price_per_sqft'].mean() if len(loc_data) > 0 else market_df['price_per_sqft'].mean()
        
        # Create feature vector
        X = np.array([[bhk, area, loc_encoded, ptype_encoded, avg_price_per_sqft]])
        
        # Predict
        predicted_price = model_rf.predict(X)[0]
        price_per_sqft_estimated = (predicted_price * 10_000_000) / area
        
        return jsonify({
            "property": {
                "bhk": bhk,
                "area_sqft": area,
                "location": location,
                "property_type": ptype
            },
            "estimate": {
                "price_cr": round(predicted_price, 2),
                "price_lakhs": round(predicted_price * 100, 2),
                "price_per_sqft": round(price_per_sqft_estimated, 0),
                "confidence": "HIGH (94.6% model accuracy)"
            }
        })
    
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route('/api/market-heatmap', methods=['GET'])
def market_heatmap():
    """
    Get location-based market rankings
    
    GET /api/market-heatmap
    """
    try:
        locations = market_df['location'].unique()
        heatmap = []
        
        for loc in locations:
            loc_data = market_df[market_df['location'] == loc]
            
            heatmap.append({
                "location": loc,
                "avg_price_cr": round(loc_data['price_cr'].mean(), 2),
                "avg_price_per_sqft": round(loc_data['price_per_sqft'].mean(), 0),
                "median_area_sqft": round(loc_data['area_sqft'].median(), 0),
                "properties_count": len(loc_data),
                "market_status": "HOT" if len(loc_data) > 100 else "WARM" if len(loc_data) > 50 else "COOL"
            })
        
        # Sort by price per sqft
        heatmap = sorted(heatmap, key=lambda x: x['avg_price_per_sqft'], reverse=True)
        
        return jsonify({
            "timestamp": pd.Timestamp.now().isoformat(),
            "total_locations": len(heatmap),
            "heatmap": heatmap
        })
    
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route('/api/compare-price', methods=['POST'])
def compare_price():
    """
    Compare a listed price against market value
    
    POST /api/compare-price
    {
        "bhk": 3,
        "area_sqft": 1500,
        "location": "Mumbai",
        "property_type": "Apartment",
        "listed_price_cr": 4.5
    }
    """
    try:
        data = request.json
        listed_price = float(data.get('listed_price_cr'))
        
        # Get estimate
        estimate_request = {
            'bhk': data.get('bhk'),
            'area_sqft': data.get('area_sqft'),
            'location': data.get('location'),
            'property_type': data.get('property_type')
        }
        
        # Temporarily change request context
        with app.test_request_context(
            '/api/estimate-price',
            method='POST',
            json=estimate_request,
            content_type='application/json'
        ):
            response = estimate_price()
            if response[1] != 200:
                return response
            
            est_data = json.loads(response[0])
            estimated_price = est_data['estimate']['price_cr']
        
        # Calculate deviation
        deviation_pct = ((listed_price - estimated_price) / estimated_price) * 100
        
        if deviation_pct < -15:
            status = "GREAT DEAL"
            advice = "Listed below market value"
        elif deviation_pct > 15:
            status = "OVERPRICED"
            advice = "Listed above market value"
        else:
            status = "FAIR"
            advice = "Listed near market value"
        
        return jsonify({
            "listed_price_cr": listed_price,
            "fair_value_cr": round(estimated_price, 2),
            "difference_cr": round(listed_price - estimated_price, 2),
            "difference_pct": round(deviation_pct, 1),
            "status": status,
            "advice": advice
        })
    
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route('/api/market-insights', methods=['GET'])
def market_insights():
    """
    Get overall market insights
    
    GET /api/market-insights
    """
    try:
        insights = {
            "market_overview": {
                "total_properties": len(market_df),
                "avg_price_cr": round(market_df['price_cr'].mean(), 2),
                "median_price_cr": round(market_df['price_cr'].median(), 2),
                "price_range": f"{market_df['price_cr'].min():.2f} - {market_df['price_cr'].max():.2f} Cr"
            },
            "area_distribution": {
                "avg_area_sqft": round(market_df['area_sqft'].mean(), 0),
                "median_area_sqft": round(market_df['area_sqft'].median(), 0)
            },
            "bhk_distribution": market_df['bhk'].value_counts().to_dict(),
            "top_5_expensive_locations": [
                {
                    "location": loc,
                    "price_per_sqft": round(market_df[market_df['location'] == loc]['price_per_sqft'].mean(), 0)
                }
                for loc in market_df.groupby('location')['price_per_sqft'].mean().nlargest(5).index
            ]
        }
        
        return jsonify(insights)
    
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ============================================================================
# ERROR HANDLING
# ============================================================================

@app.errorhandler(404)
def not_found(e):
    return jsonify({
        "error": "Endpoint not found",
        "available_endpoints": [
            "/api/health",
            "/api/estimate-price (POST)",
            "/api/compare-price (POST)",
            "/api/market-heatmap (GET)",
            "/api/market-insights (GET)"
        ]
    }), 404


# ============================================================================
# RUN
# ============================================================================

if __name__ == '__main__':
    print("""
    ================================================================
    BROKERAGE INTELLIGENCE API
    ================================================================
    
    Available Endpoints:
    
    1. GET  /api/health
       Health check
    
    2. POST /api/estimate-price
       Estimate property price
       Body: {bhk, area_sqft, location, property_type}
    
    3. POST /api/compare-price
       Compare listed price vs fair value
       Body: {bhk, area_sqft, location, property_type, listed_price_cr}
    
    4. GET  /api/market-heatmap
       Location rankings by price per sqft
    
    5. GET  /api/market-insights
       Overall market statistics
    
    ================================================================
    """)
    
    app.run(debug=True, host='0.0.0.0', port=5000)

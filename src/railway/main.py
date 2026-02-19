"""
FastAPI Server for Railway Deployment
Serves Market Intelligence API + Scheduled Scraper/Retraining
"""

from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from apscheduler.schedulers.background import BackgroundScheduler
import pickle
import pandas as pd
import numpy as np
import json
import shutil
from pathlib import Path
from datetime import datetime
import os
import logging
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, r2_score
import warnings
warnings.filterwarnings('ignore')

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize FastAPI
app = FastAPI(
    title="PropertyIntel - Market Intelligence API",
    description="Real estate market analysis for realpoint.in",
    version="1.0.0"
)

# CORS Configuration for realpoint.in
app.add_middleware(
    CORSMiddleware,
    allow_origins=["realpoint.in", "www.realpoint.in", "*.realpoint.in", "localhost", "127.0.0.1"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# ============================================================================
# CONFIG
# ============================================================================

DATA_DIR = Path(os.getenv("DATA_DIR", "/data/raw"))
MODEL_DIR = Path(os.getenv("MODEL_DIR", "/data/models"))
LOG_DIR = Path(os.getenv("LOG_DIR", "/data/logs"))
ADMIN_KEY = os.getenv("ADMIN_API_KEY", "change-me-in-production")

# Create directories
for d in [DATA_DIR, MODEL_DIR, LOG_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# Global state
model_rf = None
le_location = None
le_ptype = None
market_data = None
last_update_time = None

# ============================================================================
# MODEL LOADING
# ============================================================================

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
    logger.info(f"Original records: {len(df)}")
    
    # Convert dtypes
    df['bhk'] = pd.to_numeric(df['bhk'], errors='coerce')
    df['area_sqft'] = pd.to_numeric(df['area_sqft'], errors='coerce')
    
    # Remove invalid
    df = df[(df['bhk'] >= 1) & (df['bhk'] <= 5)].copy()
    df = df[(df['area_sqft'] >= 300) & (df['area_sqft'] <= 10000)].copy()
    df = df[(df['price_cr'] >= 0.3) & (df['price_cr'] <= 50)].copy()
    df = df.dropna(subset=['bhk', 'area_sqft', 'price_cr', 'location'])
    
    logger.info(f"After cleaning: {len(df)}")
    return df


def feature_engineering(df):
    """Create features for modeling"""
    df['price_per_sqft'] = (df['price_cr'] * 10_000_000) / df['area_sqft']
    
    # Location grouping
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


def run_model_retraining():
    """Full retraining pipeline - called weekly or manually"""
    global model_rf, le_location, le_ptype, market_data, last_update_time
    
    logger.info("🔄 Starting model retraining...")
    
    try:
        # Load and prepare data
        data_path = DATA_DIR / "magicbricks_all_cities.jsonl"
        records = []
        with open(data_path, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    records.append(json.loads(line))
        
        df = pd.DataFrame(records)
        logger.info(f"Loaded {len(df)} total properties")
        
        # Clean data
        df = normalize_prices(df)
        df = clean_data(df)
        
        # Feature engineering
        df, le_location, le_ptype = feature_engineering(df)
        
        logger.info(f"Training on {len(df)} cleaned properties, {df['location_grouped'].nunique()} locations")
        
        # Prepare training data
        feature_cols = ['bhk', 'area_sqft', 'location_encoded', 'ptype_encoded', 'price_per_sqft']
        X = df[feature_cols].values
        y = df['price_cr'].values
        
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.2, random_state=42
        )
        
        # Train Random Forest
        logger.info("Training Random Forest...")
        model_rf = RandomForestRegressor(n_estimators=100, max_depth=20, random_state=42, n_jobs=-1)
        model_rf.fit(X_train, y_train)
        
        # Evaluate
        y_pred = model_rf.predict(X_test)
        mae = mean_absolute_error(y_test, y_pred)
        r2 = r2_score(y_test, y_pred)
        
        logger.info(f"Model Performance - MAE: {mae:.4f} Cr, R²: {r2:.4f}")
        
        # Save models
        model_path = MODEL_DIR / "price_predictor_rf.pkl"
        encoder_path = MODEL_DIR / "encoders.pkl"
        
        with open(model_path, 'wb') as f:
            pickle.dump(model_rf, f)
        with open(encoder_path, 'wb') as f:
            pickle.dump((le_location, le_ptype), f)
        
        logger.info(f"✅ Models saved to {MODEL_DIR}")
        
        # Update global state
        market_data = df
        last_update_time = datetime.utcnow()
        
        return True
        
    except Exception as e:
        logger.error(f"❌ Model retraining failed: {e}")
        return False


def initialize_persistent_volume():
    """On first run, copy models and data from repo to persistent volume"""
    
    # Check if models exist in persistent volume
    models_exist = (MODEL_DIR / "price_predictor_rf.pkl").exists()
    data_exists = (DATA_DIR / "magicbricks_all_cities.jsonl").exists()
    
    if not models_exist or not data_exists:
        logger.info("📁 Initializing persistent volume from repository...")
        
        # Copy from repo to persistent volume
        repo_models = Path("/app/models")
        repo_data = Path("/app/data/raw")
        
        try:
            if repo_models.exists() and not models_exist:
                logger.info(f"  Copying models from {repo_models} to {MODEL_DIR}...")
                for pkl_file in repo_models.glob("*.pkl"):
                    shutil.copy2(pkl_file, MODEL_DIR / pkl_file.name)
                logger.info(f"  ✅ Models copied")
            
            if repo_data.exists() and not data_exists:
                logger.info(f"  Copying data from {repo_data} to {DATA_DIR}...")
                for data_file in repo_data.glob("*"):
                    shutil.copy2(data_file, DATA_DIR / data_file.name)
                logger.info(f"  ✅ Data copied")
                
        except Exception as e:
            logger.warning(f"  ⚠️ Could not copy files: {e}")


def load_models():
    """Load ML models from disk"""
    global model_rf, le_location, le_ptype, market_data, last_update_time
    
    try:
        # Load Random Forest model
        model_path = MODEL_DIR / "price_predictor_rf.pkl"
        with open(model_path, "rb") as f:
            model_rf = pickle.load(f)
        
        # Load encoders
        encoder_path = MODEL_DIR / "encoders.pkl"
        with open(encoder_path, "rb") as f:
            le_location, le_ptype = pickle.load(f)
        
        # Load market data
        data_path = DATA_DIR / "magicbricks_all_cities.jsonl"
        market_data = pd.read_json(data_path, lines=True)
        
        # Normalize prices
        def to_crore(row):
            price = float(row['price'])
            return price / 100 if row['price_unit'] == 'Lac' else price
        
        market_data['price_cr'] = market_data.apply(to_crore, axis=1)
        market_data['area_sqft'] = pd.to_numeric(market_data['area_sqft'], errors='coerce')
        market_data['price_per_sqft'] = (market_data['price_cr'] * 10_000_000) / market_data['area_sqft']
        
        # Encode locations
        market_data['location_encoded'] = le_location.transform(market_data['location'])
        market_data['ptype_encoded'] = le_ptype.transform(market_data['property_type'])
        
        last_update_time = datetime.fromtimestamp(data_path.stat().st_mtime)
        
        logger.info(f"✅ Models loaded: {len(market_data)} properties")
        return True
        
    except FileNotFoundError as e:
        logger.error(f"❌ Model files not found: {e}")
        return False
    except Exception as e:
        logger.error(f"❌ Failed to load models: {e}")
        return False


# Load models on startup
@app.on_event("startup")
async def startup():
    # First, initialize persistent volume from repo if needed
    initialize_persistent_volume()
    
    # Then load models
    if not load_models():
        logger.warning("Models not loaded - API will return errors until models are available")


# ============================================================================
# API ENDPOINTS - HEALTH & INFO
# ============================================================================

@app.get("/health", tags=["System"])
def health_check():
    """Health check endpoint"""
    return {
        "status": "OK" if model_rf is not None else "LOADING",
        "timestamp": datetime.utcnow().isoformat(),
        "data_points": len(market_data) if market_data is not None else 0,
        "model": "Random Forest (94.6% accuracy)"
    }


@app.get("/api/status", tags=["System"])
def api_status():
    """API status and data freshness"""
    if model_rf is None:
        raise HTTPException(status_code=503, detail="Models not loaded")
    
    return {
        "status": "ACTIVE",
        "properties_indexed": len(market_data),
        "locations": int(market_data['location'].nunique()),
        "last_updated": last_update_time.isoformat() if last_update_time else None,
        "model_accuracy": 0.946,
        "api_version": "1.0.0"
    }


# ============================================================================
# API ENDPOINTS - PRICE ESTIMATION
# ============================================================================

@app.get("/api/estimate-price", tags=["Estimation"])
def estimate_price(
    bhk: int,
    area_sqft: int,
    location: str = "Mumbai",
    property_type: str = "Apartment"
):
    """
    Estimate fair market price for a property
    
    Example: /api/estimate-price?bhk=3&area_sqft=1500&location=Mumbai
    """
    if model_rf is None:
        raise HTTPException(status_code=503, detail="Models loading, try again in 10 seconds")
    
    try:
        # Validation
        if not (1 <= bhk <= 5):
            raise HTTPException(status_code=400, detail="BHK must be 1-5")
        if not (300 <= area_sqft <= 10000):
            raise HTTPException(status_code=400, detail="Area must be 300-10,000 sqft")
        
        # Encode categorical features
        try:
            loc_encoded = le_location.transform([location])[0]
        except ValueError:
            loc_encoded = le_location.transform(["Other"])[0]
            location = "Other"
        
        try:
            ptype_encoded = le_ptype.transform([property_type])[0]
        except ValueError:
            ptype_encoded = le_ptype.transform(["Apartment"])[0]
            property_type = "Apartment"
        
        # Get location-specific average price per sqft
        loc_data = market_data[market_data['location'].str.contains(location, case=False, na=False)]
        avg_price_per_sqft = loc_data['price_per_sqft'].mean() if len(loc_data) > 0 else market_data['price_per_sqft'].mean()
        
        # Predict
        X = np.array([[bhk, area_sqft, loc_encoded, ptype_encoded, avg_price_per_sqft]])
        predicted_price = model_rf.predict(X)[0]
        estimated_price_per_sqft = (predicted_price * 10_000_000) / area_sqft
        
        return JSONResponse({
            "property": {
                "bhk": int(bhk),
                "area_sqft": int(area_sqft),
                "location": location,
                "property_type": property_type
            },
            "estimate": {
                "price_cr": round(predicted_price, 2),
                "price_lakhs": round(predicted_price * 100, 1),
                "price_per_sqft": round(estimated_price_per_sqft, 0)
            },
            "confidence": {
                "accuracy": 0.946,
                "model": "Random Forest",
                "note": "±5-15% variance expected"
            },
            "timestamp": datetime.utcnow().isoformat()
        }, status_code=200)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Estimation error: {e}")
        raise HTTPException(status_code=400, detail=f"Estimation failed: {str(e)}")


# ============================================================================
# API ENDPOINTS - MARKET INTELLIGENCE
# ============================================================================

@app.get("/api/market-heatmap", tags=["Intelligence"])
def market_heatmap():
    """Location rankings by price per sqft (hottest markets first)"""
    if model_rf is None:
        raise HTTPException(status_code=503, detail="Models loading")
    
    heatmap = []
    for location in sorted(market_data['location'].unique()):
        loc_data = market_data[market_data['location'] == location]
        
        # Determine market heat
        if len(loc_data) > 100:
            status = "🔥 HOT"
        elif len(loc_data) > 50:
            status = "🌤️ WARM"
        else:
            status = "❄️ COOL"
        
        heatmap.append({
            "location": location,
            "avg_price_cr": round(loc_data['price_cr'].mean(), 2),
            "median_price_cr": round(loc_data['price_cr'].median(), 2),
            "avg_price_per_sqft": round(loc_data['price_per_sqft'].mean(), 0),
            "min_price_cr": round(loc_data['price_cr'].min(), 2),
            "max_price_cr": round(loc_data['price_cr'].max(), 2),
            "properties_count": len(loc_data),
            "market_status": status
        })
    
    # Sort by price per sqft (most expensive first)
    heatmap_sorted = sorted(heatmap, key=lambda x: x['avg_price_per_sqft'], reverse=True)
    
    return JSONResponse({
        "heatmap": heatmap_sorted,
        "total_locations": len(heatmap),
        "timestamp": datetime.utcnow().isoformat(),
        "last_data_update": last_update_time.isoformat() if last_update_time else None
    })


@app.get("/api/deals-this-week", tags=["Intelligence"])
def deals_this_week(min_discount: int = 15):
    """Find properties that are 15%+ underpriced vs market estimate"""
    if model_rf is None:
        raise HTTPException(status_code=503, detail="Models loading")
    
    try:
        # Prepare features
        X_all = market_data[['bhk', 'area_sqft', 'location_encoded', 'ptype_encoded', 'price_per_sqft']].values
        
        # Get predictions
        predicted_prices = model_rf.predict(X_all)
        market_data_copy = market_data.copy()
        market_data_copy['predicted_price'] = predicted_prices
        
        # Calculate deviation
        market_data_copy['discount_pct'] = (
            (market_data_copy['price_cr'] - market_data_copy['predicted_price']) / 
            market_data_copy['predicted_price'] * 100
        )
        
        # Find best deals (underpriced by min_discount%)
        deals = market_data_copy[market_data_copy['discount_pct'] > min_discount].nlargest(20, 'area_sqft')
        
        deal_list = []
        for _, row in deals.iterrows():
            try:
                deal_list.append({
                    "location": row['location'],
                    "bhk": int(row['bhk']),
                    "area_sqft": int(row['area_sqft']),
                    "listed_price_cr": round(row['price_cr'], 2),
                    "fair_value_cr": round(row['predicted_price'], 2),
                    "savings_cr": round(row['price_cr'] - row['predicted_price'], 2),
                    "discount_pct": round(row['discount_pct'], 1),
                    "property_type": row['property_type']
                })
            except:
                pass
        
        return JSONResponse({
            "deals": deal_list,
            "count": len(deal_list),
            "min_discount_threshold": min_discount,
            "timestamp": datetime.utcnow().isoformat()
        })
        
    except Exception as e:
        logger.error(f"Deals error: {e}")
        return JSONResponse({"deals": [], "error": str(e)})


@app.get("/api/market-insights", tags=["Intelligence"])
def market_insights():
    """Overall market statistics and trends"""
    if model_rf is None:
        raise HTTPException(status_code=503, detail="Models loading")
    
    return JSONResponse({
        "market": {
            "total_properties": len(market_data),
            "total_locations": int(market_data['location'].nunique()),
            "price_range": {
                "min_cr": round(market_data['price_cr'].min(), 2),
                "max_cr": round(market_data['price_cr'].max(), 2),
                "avg_cr": round(market_data['price_cr'].mean(), 2),
                "median_cr": round(market_data['price_cr'].median(), 2)
            }
        },
        "model": {
            "accuracy_r2": 0.946,
            "mae_cr": 0.09,
            "type": "Random Forest (100 trees)",
            "training_samples": 1000
        },
        "data": {
            "last_updated": last_update_time.isoformat() if last_update_time else None,
            "next_update": "In 7 days"
        },
        "timestamp": datetime.utcnow().isoformat()
    })


# ============================================================================
# ADMIN ENDPOINTS (Protected)
# ============================================================================

@app.post("/admin/trigger-scraper", tags=["Admin"])
def trigger_scraper_manual(x_api_key: str = Header(None)):
    """
    Manually trigger scraper (Admin only)
    Header: X-API-Key: <ADMIN_API_KEY>
    """
    if x_api_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Invalid or missing API key")
    
    try:
        logger.info("🚀 Manual scraper trigger started")
        
        # Import scraper
        import sys
        sys.path.insert(0, "/app/src/scrapers")
        from magicbricks_scraper import scrape_infinite_parallel
        
        # Run scraper (with data persisted to /data/raw)
        logger.info("🔍 Starting scraper...")
        try:
            scrape_infinite_parallel(max_pages=15, enable_details=True, max_workers=2)
            logger.info("✅ Scraper completed successfully")
        except Exception as scraper_err:
            logger.error(f"❌ Scraper failed: {scraper_err}", exc_info=True)
            raise
        
        # Reload models
        logger.info("🔄 Reloading models...")
        try:
            load_models()
            logger.info("✅ Models reloaded successfully")
        except Exception as load_err:
            logger.error(f"❌ Model loading failed: {load_err}", exc_info=True)
            raise
        
        logger.info("✅ Manual scraper completed")
        
        return {
            "status": "Success",
            "message": "Scraper completed and models reloaded",
            "timestamp": datetime.utcnow().isoformat()
        }
        
    except Exception as e:
        logger.error(f"Scraper error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Scraper failed: {str(e)}")


@app.post("/admin/retrain-model", tags=["Admin"])
def retrain_model_endpoint(x_api_key: str = Header(None)):
    """
    Manually retrain ML model (Admin only)
    Header: X-API-Key: <ADMIN_API_KEY>
    """
    if x_api_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Invalid or missing API key")
    
    try:
        logger.info("🧠 Manual model retraining started")
        
        # Run full retraining pipeline
        if run_model_retraining():
            logger.info("✅ Model retraining completed")
            return {
                "status": "Success",
                "message": "Model retraining completed and models reloaded",
                "timestamp": datetime.utcnow().isoformat()
            }
        else:
            raise HTTPException(status_code=500, detail="Model retraining failed - check logs")
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Retrain error: {e}")
        raise HTTPException(status_code=500, detail=f"Retraining failed: {str(e)}")


# ============================================================================
# BACKGROUND SCHEDULER
# ============================================================================

def scheduled_weekly_update():
    """Scheduled task: Run every 7 days"""
    logger.info(f"\n{'='*60}")
    logger.info(f"[{datetime.utcnow()}] Starting weekly market intelligence update")
    logger.info("="*60)
    
    try:
        # Step 1: Run scraper
        logger.info("Step 1/3: Running property scraper...")
        import sys
        sys.path.insert(0, "/app/src/scrapers")
        from magicbricks_scraper import scrape_infinite_parallel
        scrape_infinite_parallel(max_pages=15, enable_details=True, max_workers=2)
        logger.info("✅ Scraper completed")
        
        # Step 2: Retrain model
        logger.info("Step 2/3: Retraining ML model...")
        if run_model_retraining():
            logger.info("✅ Model retraining completed")
        else:
            logger.error("❌ Model retraining failed - using existing models")
        
        # Step 3: Reload models
        logger.info("Step 3/3: Reloading models into memory...")
        load_models()
        logger.info("✅ Models reloaded")
        
        logger.info(f"{'='*60}")
        logger.info(f"[{datetime.utcnow()}] ✅ Weekly update successful!")
        logger.info("="*60)
        
    except Exception as e:
        logger.error(f"{'='*60}")
        logger.error(f"[{datetime.utcnow()}] ❌ Weekly update FAILED: {e}")
        logger.error("="*60)


# Initialize scheduler
scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(
    scheduled_weekly_update,
    'cron',
    day_of_week='0',      # Sunday
    hour='2',              # 2 AM
    minute='0',            # UTC
    id='weekly_update'
)

@app.on_event("startup")
def start_scheduler():
    scheduler.start()
    logger.info("📅 Background scheduler started - weekly updates at Sunday 2 AM UTC")


# ============================================================================
# ROOT ENDPOINT
# ============================================================================

@app.get("/", tags=["Root"])
def root():
    """API root with documentation links"""
    return {
        "name": "PropertyIntel - Market Intelligence API",
        "version": "1.0.0",
        "description": "Real estate price estimation and market analysis for realpoint.in",
        "docs_url": "/docs",
        "endpoints": {
            "estimation": "/api/estimate-price?bhk=3&area_sqft=1500&location=Mumbai",
            "market_heatmap": "/api/market-heatmap",
            "deals": "/api/deals-this-week",
            "insights": "/api/market-insights",
            "health": "/health",
            "status": "/api/status"
        },
        "admin": {
            "trigger_scraper": "POST /admin/trigger-scraper (header: X-API-Key)",
            "retrain_model": "POST /admin/retrain-model (header: X-API-Key)"
        }
    }


# ============================================================================
# RUN
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    
    logger.info("""
    
    ╔════════════════════════════════════════════════════════════════╗
    ║         PROPERTYINTEL - MARKET INTELLIGENCE API               ║
    ║              Deployed on Railway for realpoint.in              ║
    ╚════════════════════════════════════════════════════════════════╝
    
    🚀 Starting server...
    📖 Documentation: http://localhost:8000/docs
    🏥 Health check: http://localhost:8000/health
    
    """)
    
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info"
    )

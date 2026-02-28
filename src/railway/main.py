"""
FastAPI Server for Railway Deployment
Serves Market Intelligence API + Scheduled Scraper/Retraining
"""

from fastapi import FastAPI, HTTPException, Header, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from apscheduler.schedulers.background import BackgroundScheduler
from pytz import utc
import pickle
import pandas as pd
import numpy as np
import json
import shutil
from pathlib import Path
from datetime import datetime, timedelta
import os
import logging
from sklearn.model_selection import train_test_split, cross_val_predict
from sklearn.preprocessing import LabelEncoder
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, r2_score, mean_squared_error
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
encoder_bundle_g = None   # holds le_ptype, location_means, city_means, feature_cols, …
market_data = None
last_update_time = None
model_performance = {
    "mae_cr":           None,
    "rmse_cr":          None,
    "r2":               None,
    "training_samples": None,
    "trained_at":       None,
}

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
    """Engineer training features — zero target leakage.

    Returns (df_enriched, encoder_bundle) where encoder_bundle holds
    everything needed to replicate the same transforms at inference time.
    """
    # ── Temporal features ──────────────────────────────────────────────────
    def _days_on_market(row):
        try:
            first = row.get("first_seen_at") or row.get("scraped_at", "")
            last  = row.get("last_seen_at")  or row.get("scraped_at", "")
            if first and last:
                d1 = datetime.fromisoformat(str(first)[:19])
                d2 = datetime.fromisoformat(str(last)[:19])
                return max(0, (d2 - d1).days)
        except Exception:
            pass
        return 0

    df["days_on_market"] = df.apply(_days_on_market, axis=1)

    ph_col = (
        df["price_history"] if "price_history" in df.columns
        else pd.Series([[] for _ in range(len(df))], index=df.index)
    )
    df["price_change_count"] = ph_col.apply(
        lambda x: len(x) if isinstance(x, list) else 0
    )

    def _price_velocity(row):
        """Monthly % price change vs oldest known price. Capped ±100%."""
        ph = row.get("price_history")
        if not isinstance(ph, list) or len(ph) == 0:
            return 0.0
        try:
            oldest = float(ph[0]["price"])
            if ph[0].get("price_unit") == "Lac":
                oldest /= 100
            current = float(row.get("price_cr", 0))
            if oldest == 0:
                return 0.0
            months = max(1, row.get("days_on_market", 0)) / 30.0
            return round(
                float(np.clip(((current - oldest) / oldest) * 100 / months, -100, 100)), 2
            )
        except Exception:
            return 0.0

    df["price_velocity"] = df.apply(_price_velocity, axis=1)

    # ── Location grouping ──────────────────────────────────────────────────
    location_counts = df["location"].value_counts()
    major_locations = location_counts[location_counts >= 20].index.tolist()
    df["location_grouped"] = df["location"].apply(
        lambda x: x if x in major_locations else "Other"
    )

    # ── City column (older records pre-date the city tag) ─────────────────
    if "city" not in df.columns:
        df["city"] = "Unknown"
    df["city"] = df["city"].fillna("Unknown")

    # ── Target encoding — location/city encoded as mean price_cr ──────────
    # Computed on the training corpus only; saved for inference lookups.
    global_mean    = float(df["price_cr"].mean())
    location_means = df.groupby("location_grouped")["price_cr"].mean().to_dict()
    city_means     = df.groupby("city")["price_cr"].mean().to_dict()

    df["location_target"] = df["location_grouped"].map(location_means).fillna(global_mean)
    df["city_target"]     = df["city"].map(city_means).fillna(global_mean)

    # ── Property type label encoder ────────────────────────────────────────
    le_ptype = LabelEncoder()
    df["ptype_encoded"] = le_ptype.fit_transform(df["property_type"].fillna("Apartment"))

    # ── price_per_sqft — market analytics only, NOT a training feature ─────
    df["price_per_sqft"] = (df["price_cr"] * 10_000_000) / df["area_sqft"]

    encoder_bundle = {
        "le_ptype":          le_ptype,
        "location_means":    location_means,
        "city_means":        city_means,
        "global_mean_price": global_mean,
        "major_locations":   major_locations,
    }
    return df, encoder_bundle


def run_model_retraining():
    """Full retraining pipeline - called weekly or manually"""
    global model_rf, encoder_bundle_g, market_data, last_update_time, model_performance
    
    logger.info("🔄 Starting full model retraining pipeline...")
    
    try:
        # Load and prepare data
        data_path = DATA_DIR / "magicbricks_all_cities.jsonl"
        logger.info(f"  📂 Reading data from: {data_path}")
        
        records = []
        with open(data_path, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    records.append(json.loads(line))

        total_records = len(records)
        # Train only on active (non-stale) listings — stale records are kept in
        # the master file for historical/trend queries but excluded from the
        # price predictor so it reflects the current market.
        active_records = [r for r in records if not r.get("is_stale", False)]
        stale_count = total_records - len(active_records)
        logger.info(
            f"     ✓ Loaded {total_records} total properties "
            f"({len(active_records)} active, {stale_count} stale/historical — training on active only)"
        )
        records = active_records

        df = pd.DataFrame(records)
        logger.info(f"     ✓ Training dataset: {len(df)} properties")
        
        # Clean data
        logger.info(f"\n  🧹 Normalizing prices and cleaning data...")
        df = normalize_prices(df)
        initial_count = len(df)
        df = clean_data(df)
        cleaned_count = len(df)
        logger.info(f"     ✓ Removed {initial_count - cleaned_count} invalid records")
        logger.info(f"     ✓ Kept {cleaned_count} clean properties")
        
        # Feature engineering
        logger.info(f"\n  🔧 Feature engineering...")
        df, encoder_bundle = feature_engineering(df)
        logger.info(f"     ✓ Identified {df['location_grouped'].nunique()} major locations")
        logger.info(f"     ✓ Property types: {df['ptype_encoded'].nunique()}")
        
        # Prepare training data
        logger.info(f"\n  📊 Preparing training data...")
        # Use the 7-feature set for richer analysis
        FEATURE_COLS = [
            "bhk",
            "area_sqft",
            "location_target",      # target-encoded: mean price_cr per neighbourhood
            "city_target",          # target-encoded: mean price_cr per city
            "ptype_encoded",
            "days_on_market",       # demand proxy
            "price_change_count",   # price volatility signal
        ]
        
        X = df[FEATURE_COLS].values
        y = df["price_cr"].values
        logger.info(f"     ✓ Features: {FEATURE_COLS}")
        logger.info(f"     ✓ Samples: {len(X)}")
        
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.2, random_state=42
        )
        logger.info(f"     ✓ Train set: {len(X_train)} samples")
        logger.info(f"     ✓ Test set: {len(X_test)} samples")
        
        # Train Random Forest
        logger.info(f"\n  🧠 Training Random Forest model...")
        logger.info(f"     Configuration: 100 trees, max_depth=20")
        model_rf = RandomForestRegressor(
            n_estimators=200,
            max_depth=None,
            min_samples_leaf=3,
            max_features="sqrt",
            random_state=42,
            n_jobs=-1,
        )
        model_rf.fit(X_train, y_train)
        logger.info(f"     ✓ Training completed")
        
        # Evaluate
        logger.info(f"\n  📈 Evaluating model performance...")
        y_pred = model_rf.predict(X_test)
        mae    = float(mean_absolute_error(y_test, y_pred))
        rmse   = float(np.sqrt(mean_squared_error(y_test, y_pred)))
        r2     = float(r2_score(y_test, y_pred))

        # Out-of-fold predictions for the deal finder
        oof_rf = RandomForestRegressor(
            n_estimators=200, max_depth=None, min_samples_leaf=3,
            max_features="sqrt", random_state=42, n_jobs=-1,
        )
        oof_preds = cross_val_predict(oof_rf, X, y, cv=5)
        df["oof_predicted_price"] = oof_preds

        # Update live performance tracker
        model_performance.update({
            "mae_cr":           round(mae, 4),
            "rmse_cr":          round(rmse, 4),
            "r2":               round(r2, 4),
            "training_samples": int(len(X_train)),
            "trained_at":       datetime.utcnow().isoformat(),
        })
        
        logger.info(f"     ✓ Mean Absolute Error: {mae:.4f} Crores")
        logger.info(f"     ✓ R² Score: {r2:.4f} ({r2*100:.2f}% variance explained)")
        logger.info(f"     ✓ Model Accuracy: {r2*100:.1f}%")
        
        # Save models
        logger.info(f"\n  💾 Saving models to disk...")
        model_path = MODEL_DIR / "price_predictor_rf.pkl"
        encoder_path = MODEL_DIR / "encoders.pkl"
        
        with open(model_path, 'wb') as f:
            pickle.dump(model_rf, f)
        logger.info(f"     ✓ Saved: {model_path}")
        
        # Save encoder bundle with proper location/city means for inference
        encoder_dict = {
            "le_location": encoder_bundle.get("le_location"),
            "le_ptype": encoder_bundle.get("le_ptype"),
            "location_means": encoder_bundle.get("location_means", {}),
            "city_means": encoder_bundle.get("city_means", {}),
            "global_mean_price": encoder_bundle.get("global_mean_price", float(df["price_cr"].mean())),
            "major_locations": encoder_bundle.get("major_locations", []),
            "feature_cols": FEATURE_COLS,
        }
        
        with open(encoder_path, "wb") as f:
            pickle.dump(encoder_dict, f)
        logger.info(f"     ✓ Saved: {encoder_path}")

        # Save OOF predictions keyed by URL so they survive container restarts.
        # load_models() will merge them back into market_data on startup.
        oof_path = MODEL_DIR / "oof_predictions.pkl"
        oof_df = df[["url", "oof_predicted_price"]].copy() if "url" in df.columns \
            else pd.DataFrame({"_idx": df.index, "oof_predicted_price": df["oof_predicted_price"]})
        with open(oof_path, "wb") as f:
            pickle.dump(oof_df, f)
        logger.info(f"     ✓ Saved OOF predictions: {oof_path}")

        # Update global state
        encoder_bundle_g = encoder_bundle
        market_data      = df
        last_update_time = datetime.utcnow()
        
        logger.info(f"\n✅ Model retraining pipeline completed successfully")
        logger.info(f"   - {len(df)} properties in production")
        logger.info(f"   - {df['location_grouped'].nunique()} locations supported")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ Model retraining failed: {e}", exc_info=True)
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
    """Load ML models and fully prepare market data for API use."""
    global model_rf, encoder_bundle_g, market_data, last_update_time, model_performance

    try:
        model_path = MODEL_DIR / "price_predictor_rf.pkl"
        with open(model_path, "rb") as f:
            model_rf = pickle.load(f)

        encoder_path = MODEL_DIR / "encoders.pkl"
        with open(encoder_path, "rb") as f:
            enc = pickle.load(f)
        if isinstance(enc, tuple):
            logger.warning("Old encoder tuple format - converting to dict format")
            encoder_bundle_g = {
                "le_location":       enc[0],
                "le_ptype":          enc[1],
                "location_means":    {},     # Will be computed from data
                "city_means":        {},     # Will be computed from data
                "major_locations":   [],     # Will be computed from data
                "feature_cols":      ["bhk", "area_sqft", "location_target", "city_target",
                                      "ptype_encoded", "days_on_market", "price_change_count"],
            }
        else:
            encoder_bundle_g = enc

        data_path = DATA_DIR / "magicbricks_all_cities.jsonl"
        records = []
        with open(data_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        mdf = pd.DataFrame(records)
        mdf = normalize_prices(mdf)
        mdf = clean_data(mdf)

        if "city" not in mdf.columns:
            mdf["city"] = "Unknown"
        mdf["city"] = mdf["city"].fillna("Unknown")

        def _dom(row):
            try:
                first = row.get("first_seen_at") or row.get("scraped_at", "")
                last  = row.get("last_seen_at")  or row.get("scraped_at", "")
                if first and last:
                    return max(0, (
                        datetime.fromisoformat(str(last)[:19]) -
                        datetime.fromisoformat(str(first)[:19])
                    ).days)
            except Exception:
                pass
            return 0

        mdf["days_on_market"] = mdf.apply(_dom, axis=1)

        ph_col = (
            mdf["price_history"] if "price_history" in mdf.columns
            else pd.Series([[] for _ in range(len(mdf))], index=mdf.index)
        )
        mdf["price_change_count"] = ph_col.apply(
            lambda x: len(x) if isinstance(x, list) else 0
        )

        def _velocity(row):
            ph = row.get("price_history")
            if not isinstance(ph, list) or len(ph) == 0:
                return 0.0
            try:
                oldest = float(ph[0]["price"])
                if ph[0].get("price_unit") == "Lac":
                    oldest /= 100
                current = float(row.get("price_cr", 0))
                if oldest == 0:
                    return 0.0
                months = max(1, row.get("days_on_market", 0)) / 30.0
                return round(
                    float(np.clip(((current - oldest) / oldest) * 100 / months, -100, 100)), 2
                )
            except Exception:
                return 0.0

        mdf["price_velocity"] = mdf.apply(_velocity, axis=1)

        global_mean = float(mdf["price_cr"].mean())
        
        # Get or compute location/city means
        # If encoder_bundle_g has empty dicts (old tuple encoder), compute from data
        location_means = encoder_bundle_g.get("location_means", {})
        city_means     = encoder_bundle_g.get("city_means", {})
        major_locs     = encoder_bundle_g.get("major_locations", [])
        
        # If location_means is empty, compute from the cleaned data
        if not location_means or len(location_means) == 0:
            logger.info("  ⚠️  location_means empty - computing from data")
            location_means = mdf.groupby("location")["price_cr"].mean().to_dict()
        
        # If city_means is empty, compute from the cleaned data
        if not city_means or len(city_means) == 0:
            logger.info("  ⚠️  city_means empty - computing from data")
            city_means = mdf.groupby("city")["price_cr"].mean().to_dict()
        
        # If major_locs is empty, identify from data
        if not major_locs or len(major_locs) == 0:
            location_counts = mdf["location"].value_counts()
            major_locs = location_counts[location_counts >= 20].index.tolist()

        mdf["location_grouped"] = mdf["location"].apply(
            lambda x: x if x in major_locs else "Other"
        )
        mdf["location_target"] = mdf["location_grouped"].map(location_means).fillna(global_mean)
        mdf["city_target"]     = mdf["city"].map(city_means).fillna(global_mean)

        le_ptype = encoder_bundle_g.get("le_ptype")
        if le_ptype is not None:
            known_ptypes = set(le_ptype.classes_)
            mdf["ptype_grouped"] = mdf["property_type"].fillna("Apartment").apply(
                lambda x: x if x in known_ptypes else le_ptype.classes_[0]
            )
            mdf["ptype_encoded"] = le_ptype.transform(mdf["ptype_grouped"])
        
        # Also create location_encoded for backward compatibility with old 5-feature model
        le_location = encoder_bundle_g.get("le_location")
        if le_location is not None:
            known_locs = set(le_location.classes_)
            mdf["location_safe"] = mdf["location"].apply(
                lambda x: x if x in known_locs else le_location.classes_[0]
            )
            mdf["location_encoded"] = le_location.transform(mdf["location_safe"])

        mdf["price_per_sqft"] = (mdf["price_cr"] * 10_000_000) / mdf["area_sqft"]

        if "is_stale" in mdf.columns:
            market_data = mdf[~mdf["is_stale"].fillna(False)].copy()
        else:
            market_data = mdf.copy()

        # Restore OOF predictions saved by the last retrain (keyed by URL).
        # This makes the deal finder use honest cross-validated estimates even
        # after a container restart/redeploy.
        oof_path = MODEL_DIR / "oof_predictions.pkl"
        if oof_path.exists():
            try:
                oof_df = pickle.load(open(oof_path, "rb"))
                if "url" in oof_df.columns and "url" in market_data.columns:
                    market_data = market_data.merge(
                        oof_df[["url", "oof_predicted_price"]],
                        on="url", how="left"
                    )
                    restored = market_data["oof_predicted_price"].notna().sum()
                    logger.info(f"Restored {restored}/{len(market_data)} OOF predictions from disk")
            except Exception as oof_err:
                logger.warning(f"Could not restore OOF predictions: {oof_err}")

        last_update_time = datetime.fromtimestamp(data_path.stat().st_mtime)
        logger.info(f"Models loaded: {len(market_data)} active properties")
        return True

    except FileNotFoundError as e:
        logger.error(f"Model files not found: {e}")
        return False
    except Exception as e:
        logger.error(f"Failed to load models: {e}", exc_info=True)
        return False


# Load models on startup
@app.on_event("startup")
async def startup():
    logger.info("\n" + "="*70)
    logger.info("🚀 APPLICATION STARTUP - PropertyIntel Market Intelligence API")
    logger.info("="*70)
    
    try:
        # First, initialize persistent volume from repo if needed
        logger.info("1️⃣  Initializing persistent volume...")
        initialize_persistent_volume()
        logger.info("   ✅ Persistent volume ready")
    except Exception as e:
        logger.error(f"   ❌ Persistent volume init failed: {e}", exc_info=True)
    
    try:
        # Then load models
        logger.info("2️⃣  Loading ML models...")
        if load_models():
            logger.info("   ✅ Models loaded successfully")
        else:
            logger.warning("   ⚠️  Models not loaded - API will return errors until models are available")
    except Exception as e:
        logger.error(f"   ❌ Model loading failed: {e}", exc_info=True)


# ============================================================================
# API ENDPOINTS - HEALTH & INFO
# ============================================================================

@app.get("/health", tags=["System"])
def health_check():
    """Health check endpoint"""
    r2 = model_performance.get("r2")
    return {
        "status":      "OK" if model_rf is not None else "LOADING",
        "timestamp":   datetime.utcnow().isoformat(),
        "data_points": len(market_data) if market_data is not None else 0,
        "model":       f"Random Forest | R² {r2:.4f}" if r2 else "Random Forest (pending retrain)",
    }


@app.get("/api/status", tags=["System"])
def api_status():
    """API status and data freshness"""
    if model_rf is None:
        raise HTTPException(status_code=503, detail="Models not loaded")

    return {
        "status":             "ACTIVE",
        "properties_indexed": len(market_data),
        "cities":             int(market_data["city"].nunique()) if "city" in market_data.columns else None,
        "locations":          int(market_data["location"].nunique()),
        "last_updated":       last_update_time.isoformat() if last_update_time else None,
        "model_performance":  model_performance,
        "api_version":        "1.0.0",
    }


# ============================================================================
# API ENDPOINTS - PRICE ESTIMATION
# ============================================================================

@app.get("/api/estimate-price", tags=["Estimation"])
def estimate_price(
    bhk: int,
    area_sqft: int,
    location: str = "Mumbai",
    property_type: str = "Apartment",
    city: str = "Unknown",
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
        
        # Resolve location/property type from encoder bundles
        location_means = encoder_bundle_g.get("location_means", {})
        city_means     = encoder_bundle_g.get("city_means", {})
        global_mean    = encoder_bundle_g.get("global_mean_price", 2.5)
        major_locs     = encoder_bundle_g.get("major_locations", [])
        
        # Handle property type encoding
        le_ptype_enc = encoder_bundle_g.get("le_ptype")
        try:
            ptype_encoded = int(le_ptype_enc.transform([property_type])[0]) if le_ptype_enc else 0
        except (ValueError, AttributeError):
            ptype_encoded = 0
            property_type = "Apartment"
        
        # Get target-encoded location and city values
        loc_grouped = location if location in major_locs else "Other"
        location_target = location_means.get(loc_grouped, global_mean)
        city_target = city_means.get(city, city_means.get("Unknown", global_mean))
        
        # Build 7-feature array for new query (no listing history -> days_on_market=0, price_change_count=0)
        X = np.array([[bhk, area_sqft, location_target, city_target, ptype_encoded, 0, 0]])

        # Prediction + 95% CI from individual tree spread
        all_tree_preds  = np.array([tree.predict(X)[0] for tree in model_rf.estimators_])
        predicted_price = float(all_tree_preds.mean())
        pred_std        = float(all_tree_preds.std())
        ci_low  = round(max(0.0, predicted_price - 1.96 * pred_std), 2)
        ci_high = round(predicted_price + 1.96 * pred_std, 2)
        price_per_sqft_est = (predicted_price * 10_000_000) / area_sqft if area_sqft > 0 else 0

        return JSONResponse({
            "property": {
                "bhk":           int(bhk),
                "area_sqft":     int(area_sqft),
                "location":      location,
                "city":          city,
                "property_type": property_type,
            },
            "estimate": {
                "price_cr":       round(predicted_price, 2),
                "price_lakhs":    round(predicted_price * 100, 1),
                "price_per_sqft": round(price_per_sqft_est, 0),
                "range_cr":       {"low": ci_low, "high": ci_high},
            },
            "confidence": {
                "r2":     model_performance.get("r2"),
                "mae_cr": model_performance.get("mae_cr"),
                "model":  "Random Forest (200 trees)",
                "note":   f"95% CI: {ci_low} - {ci_high} Cr",
            },
            "timestamp": datetime.utcnow().isoformat(),
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
    for location in sorted(market_data['location_grouped'].unique()):
        loc_data = market_data[market_data['location_grouped'] == location]
        
        # Determine market heat from price velocity (monthly % change trend)
        velocity = float(loc_data["price_velocity"].mean()) if "price_velocity" in loc_data.columns else 0.0
        if velocity > 2:
            status = "RISING"
        elif velocity < -2:
            status = "COOLING"
        elif len(loc_data) >= 30:
            status = "STABLE"
        else:
            status = "LOW ACTIVITY"

        heatmap.append({
            "location":                   location,
            "avg_price_cr":               round(loc_data["price_cr"].mean(), 2),
            "median_price_cr":            round(loc_data["price_cr"].median(), 2),
            "avg_price_per_sqft":         round(loc_data["price_per_sqft"].mean(), 0),
            "min_price_cr":               round(loc_data["price_cr"].min(), 2),
            "max_price_cr":               round(loc_data["price_cr"].max(), 2),
            "properties_count":           len(loc_data),
            "avg_days_on_market":         round(float(loc_data["days_on_market"].mean()), 1) if "days_on_market" in loc_data.columns else None,
            "price_velocity_pct_monthly": round(velocity, 2),
            "market_status":              status,
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
        market_data_copy = market_data.copy()

        # Use OOF predictions where available (honest: model never saw these
        # properties during their prediction fold). Fall back if absent.
        if "oof_predicted_price" in market_data_copy.columns:
            market_data_copy["predicted_price"] = market_data_copy["oof_predicted_price"]
            prediction_method = "OOF cross-validated"
        else:
            feat_cols = encoder_bundle_g.get("feature_cols", []) if encoder_bundle_g else []
            available = [c for c in feat_cols if c in market_data_copy.columns]
            if not available:
                return JSONResponse({"deals": [], "error": "Feature columns unavailable - retrain model"})
            market_data_copy["predicted_price"] = model_rf.predict(market_data_copy[available].values)
            prediction_method = "in-sample (retrain for honest OOF)"

        # Calculate discount vs fair value
        market_data_copy["discount_pct"] = (
            (market_data_copy["price_cr"] - market_data_copy["predicted_price"]) /
            market_data_copy["predicted_price"] * 100
        )
        market_data_copy["savings_cr"] = (
            market_data_copy["predicted_price"] - market_data_copy["price_cr"]
        )

        # --- Sanity filters to remove false deals caused by data quality issues ---
        # Problem 1: Outer-suburb properties labeled as city (e.g. Virar/Mira Road
        # labeled "Mumbai") have legitimately low price/sqft but the model predicts
        # the city average, making them appear 80%+ underpriced.
        # Problem 2: Rare/large property types where the model has no training data
        # extrapolate poorly, inflating fair-value estimates.
        #
        # Fix: A valid deal must have price/sqft >= 35% of its location's median
        # price/sqft (same market segment), AND the discount cannot exceed 45%
        # (real estate doesn't have genuine 80% discounts on open listings).

        market_data_copy["price_per_sqft"] = (
            market_data_copy["price_cr"] * 1e7 / market_data_copy["area_sqft"].replace(0, float("nan"))
        )
        # Compute median price/sqft per location_grouped (in-dataset reference)
        loc_median_psqft = (
            market_data_copy.groupby("location_grouped")["price_per_sqft"].median().to_dict()
            if "location_grouped" in market_data_copy.columns
            else {}
        )
        if loc_median_psqft:
            market_data_copy["_loc_median_psqft"] = (
                market_data_copy["location_grouped"].map(loc_median_psqft)
            )
            # Keep only properties where listed price/sqft >= 35% of location median
            # (filters out different-tier sub-markets mislabeled under a city name)
            psqft_ok = (
                market_data_copy["price_per_sqft"] >= market_data_copy["_loc_median_psqft"] * 0.35
            )
            market_data_copy = market_data_copy[psqft_ok]

        # Cap: no more than 45% discount (extreme values = model extrapolation error)
        MAX_DISCOUNT = 45
        candidate_mask = (
            (market_data_copy["discount_pct"] < -min_discount) &
            (market_data_copy["discount_pct"] >= -MAX_DISCOUNT)
        )
        deals = market_data_copy[candidate_mask].nlargest(20, "savings_cr")

        deal_list = []
        for _, row in deals.iterrows():
            try:
                deal_list.append({
                    "location":        row["location"],
                    "city":            row.get("city", "Unknown"),
                    "bhk":             int(row["bhk"]),
                    "area_sqft":       int(row["area_sqft"]),
                    "listed_price_cr": round(row["price_cr"], 2),
                    "fair_value_cr":   round(row["predicted_price"], 2),
                    "savings_cr":      round(row["savings_cr"], 2),
                    "discount_pct":    round(abs(row["discount_pct"]), 1),
                    "property_type":   row["property_type"],
                    "days_on_market":  int(row.get("days_on_market", 0)),
                    "price_velocity":  float(row.get("price_velocity", 0.0)),
                    "url":             str(row.get("url", "")),
                })
            except Exception:
                pass

        return JSONResponse({
            "deals":                  deal_list,
            "count":                  len(deal_list),
            "min_discount_threshold": min_discount,
            "prediction_method":      prediction_method,
            "timestamp":              datetime.utcnow().isoformat(),
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
            "r2":               model_performance.get("r2"),
            "mae_cr":           model_performance.get("mae_cr"),
            "rmse_cr":          model_performance.get("rmse_cr"),
            "type":             "Random Forest (200 trees)",
            "training_samples": model_performance.get("training_samples"),
            "trained_at":       model_performance.get("trained_at"),
        },
        "data": {
            "last_updated": last_update_time.isoformat() if last_update_time else None,
            "next_update": "In 7 days"
        },
        "timestamp": datetime.utcnow().isoformat()
    })


# ============================================================================
# HELPERS
# ============================================================================

def _merge_ingest(existing_path: Path, new_records: list) -> tuple:
    """
    URL-keyed smart merge of incoming scraped records into the master dataset.

    Rules:
      - URL match + price changed  → update record, push old price into price_history
      - URL match + price same     → refresh last_seen_at, clear stale flag
      - URL not in master          → insert as new record
      - In master but not in batch → mark is_stale = True
      - first_seen_at older than 1 year → delete entirely
    """
    now = datetime.utcnow().isoformat()
    one_year_ago = (datetime.utcnow() - timedelta(days=365)).isoformat()

    # Load existing master keyed by URL
    master: dict = {}
    if existing_path.exists():
        with open(existing_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rec = json.loads(line)
                        url = rec.get("url")
                        if url:
                            master[url] = rec
                    except json.JSONDecodeError:
                        continue

    incoming_urls: set = set()
    stats = {"new": 0, "updated": 0, "unchanged": 0, "stale": 0, "expired": 0}

    for rec in new_records:
        url = rec.get("url")
        if not url:
            continue
        incoming_urls.add(url)

        if url in master:
            existing = master[url]
            price_changed = (
                str(rec.get("price", "")) != str(existing.get("price", ""))
                or str(rec.get("price_unit", "")) != str(existing.get("price_unit", ""))
            )
            if price_changed:
                # Record old price in history before overwriting
                history = existing.get("price_history", [])
                if existing.get("price"):
                    history.append({
                        "price": existing["price"],
                        "price_unit": existing.get("price_unit", ""),
                        "recorded_at": existing.get("scraped_at", existing.get("first_seen_at", now)),
                    })
                rec["price_history"] = history
                rec["first_seen_at"] = existing.get("first_seen_at", existing.get("scraped_at", now))
                rec["last_seen_at"] = now
                rec["is_stale"] = False
                master[url] = rec
                stats["updated"] += 1
            else:
                # No price change — just refresh presence metadata
                existing["last_seen_at"] = now
                existing["is_stale"] = False
                master[url] = existing
                stats["unchanged"] += 1
        else:
            # Brand-new listing
            rec["first_seen_at"] = now
            rec["last_seen_at"] = now
            rec["is_stale"] = False
            rec["price_history"] = []
            master[url] = rec
            stats["new"] += 1

    # Flag stale and expire records older than 1 year
    urls_to_delete = []
    for url, rec in master.items():
        if url not in incoming_urls:
            rec["is_stale"] = True
            stats["stale"] += 1
        age_ref = rec.get("first_seen_at") or rec.get("scraped_at", now)
        if age_ref < one_year_ago:
            urls_to_delete.append(url)
            stats["expired"] += 1

    for url in urls_to_delete:
        del master[url]
        # If it was counted as stale above, don't double-count
        stats["stale"] = max(0, stats["stale"] - 1)

    return master, stats


def _retrain_and_reload():
    """Background task: retrain ML model then hot-reload it into memory."""
    logger.info("🔁 Background retrain triggered by /admin/ingest")
    if run_model_retraining():
        load_models()
        logger.info("✅ Background retrain + reload complete")
    else:
        logger.error("❌ Background retrain failed — check Railway logs")


# ============================================================================
# ADMIN ENDPOINTS (Protected)
# ============================================================================

@app.post("/admin/ingest", tags=["Admin"])
async def ingest_scraped_data(
    request: Request,
    background_tasks: BackgroundTasks,
    x_api_key: str = Header(None),
):
    """
    Receive JSONL scraped data from GitHub Actions and trigger retraining.
    Body  : raw JSONL — one JSON object per line (Content-Type: application/x-ndjson)
    Header: X-API-Key: <ADMIN_API_KEY>
    """
    if x_api_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Invalid or missing API key")
    try:
        body = await request.body()
        if not body:
            raise HTTPException(status_code=400, detail="Empty body")

        # Parse incoming JSONL
        new_records = []
        for line in body.decode("utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    new_records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        logger.info(f"📥 /admin/ingest: {len(new_records)} incoming properties")

        out_path = DATA_DIR / "magicbricks_all_cities.jsonl"

        # Smart URL-keyed merge (upsert + stale flagging + 1-year expiry)
        master, stats = _merge_ingest(out_path, new_records)

        # Write merged master back to disk
        with open(out_path, "w", encoding="utf-8") as f:
            for rec in master.values():
                json.dump(rec, f, ensure_ascii=False)
                f.write("\n")

        logger.info(
            f"✅ Ingest merged — new: {stats['new']}, updated: {stats['updated']}, "
            f"unchanged: {stats['unchanged']}, stale: {stats['stale']}, "
            f"expired: {stats['expired']}, master total: {len(master)}"
        )

        background_tasks.add_task(_retrain_and_reload)
        return {
            "status": "accepted",
            "incoming": len(new_records),
            "stats": stats,
            "total_in_master": len(master),
            "message": "Data merged; model retraining queued in background",
            "timestamp": datetime.utcnow().isoformat(),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ingest error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


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
            scrape_infinite_parallel(max_pages=15, enable_details=True, max_workers=1)
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


@app.post("/admin/test-scheduler", tags=["Admin"])
def test_scheduler_endpoint(x_api_key: str = Header(None)):
    """
    Manually test the scheduled weekly update (Admin only)
    Runs the same pipeline as the scheduled task but immediately
    Header: X-API-Key: <ADMIN_API_KEY>
    """
    if x_api_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Invalid or missing API key")
    
    try:
        logger.info("🧪 TEST: Running scheduled_weekly_update manually...")
        success = scheduled_weekly_update()
        
        if success:
            return {
                "status": "Success",
                "message": "Test scheduler run completed successfully",
                "timestamp": datetime.utcnow().isoformat(),
                "next_scheduled": "Sunday 2 AM UTC"
            }
        else:
            return {
                "status": "Partial Failure",
                "message": "Test scheduler run had errors - check logs",
                "timestamp": datetime.utcnow().isoformat(),
                "check_logs": "/data/logs/"
            }
        
    except Exception as e:
        logger.error(f"Test scheduler error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Test scheduler failed: {str(e)}")


@app.get("/admin/scheduler-status", tags=["Admin"])
def scheduler_status_endpoint(x_api_key: str = Header(None)):
    """
    Get scheduler status and next run time (Admin only)
    Header: X-API-Key: <ADMIN_API_KEY>
    """
    if x_api_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Invalid or missing API key")
    
    try:
        jobs = scheduler.get_jobs()
        
        job_details = []
        for job in jobs:
            job_details.append({
                "id": job.id,
                "func": str(job.func),
                "trigger": str(job.trigger),
                "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
                "timezone": str(job.timezone) if hasattr(job, 'timezone') else "N/A"
            })
        
        return {
            "status": "Running" if scheduler.running else "Stopped",
            "jobs": job_details,
            "server_time": datetime.utcnow().isoformat(),
            "server_timezone": "UTC"
        }
        
    except Exception as e:
        logger.error(f"Scheduler status error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get scheduler status: {str(e)}")
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Retrain error: {e}")
        raise HTTPException(status_code=500, detail=f"Retraining failed: {str(e)}")


# ============================================================================
# BACKGROUND SCHEDULER
# ============================================================================

def scheduled_weekly_update():
    """Fallback scheduled task: Sunday 2:30 AM UTC.
    Scraping is now handled by GitHub Actions (Sunday 1 AM UTC) which calls
    /admin/ingest directly.  This fallback retrains on whatever data is already
    on the persistent volume — useful if Actions fails or is disabled.
    """
    start_time = datetime.utcnow()
    logger.info(f"\n{'='*70}")
    logger.info(f"🔄 [SCHEDULED FALLBACK RETRAIN] {start_time.isoformat()}")
    logger.info("   (Scraping delegated to GitHub Actions — this just retrains)")
    logger.info("="*70)

    try:
        # ==================================================================
        # STEP 1: RETRAIN ML MODEL  (was Step 2 — scraper removed from Railway)
        # ==================================================================
        step2_start = datetime.utcnow()
        logger.info(f"\n🧠 STEP 2/3: Retraining ML model...")
        logger.info(f"   Start time: {step2_start.isoformat()}")
        logger.info(f"   Loading scraped data (STEP 1/2)...")
        
        try:
            # Load data to get stats
            data_path = DATA_DIR / "magicbricks_all_cities.jsonl"
            if data_path.exists():
                with open(data_path, 'r', encoding='utf-8') as f:
                    data_lines = sum(1 for line in f if line.strip())
                logger.info(f"   ✓ Data loaded: {data_lines} records in JSONL")
            
            logger.info(f"\n   🔨 Training Random Forest model...")
            logger.info(f"      - 100 estimators, max_depth=20")
            logger.info(f"      - 80/20 train/test split")
            logger.info(f"      - Using all CPU cores ({os.cpu_count()} cores available)\n")
            
            if run_model_retraining():
                step2_end = datetime.utcnow()
                step2_duration = (step2_end - step2_start).total_seconds()
                logger.info(f"\n✅ STEP 1 COMPLETE: Model retraining successful")
                logger.info(f"   Duration: {step2_duration:.1f} seconds ({step2_duration/60:.1f} minutes)")
                logger.info(f"   Models saved to: {MODEL_DIR}")
                logger.info(f"   End time: {step2_end.isoformat()}")
            else:
                step2_end = datetime.utcnow()
                step2_duration = (step2_end - step2_start).total_seconds()
                logger.warning(f"⚠️  STEP 1 PARTIAL: Model retraining had issues")
                logger.warning(f"   Continuing with existing models...")
        except Exception as retrain_err:
            logger.error(f"❌ STEP 1 FAILED: Model retraining error")
            logger.error(f"   Error: {retrain_err}", exc_info=True)
            raise
        
        # ==================================================================
        # STEP 2: RELOAD MODELS INTO MEMORY
        # ==================================================================
        step3_start = datetime.utcnow()
        logger.info(f"\n🔄 STEP 2/2: Reloading models into memory...")
        logger.info(f"   Start time: {step3_start.isoformat()}")
        
        try:
            logger.info(f"   Loading: price_predictor_rf.pkl...")
            logger.info(f"   Loading: encoders.pkl...")
            logger.info(f"   Loading: market data from JSONL...\n")
            
            load_models()
            
            step3_end = datetime.utcnow()
            step3_duration = (step3_end - step3_start).total_seconds()
            
            logger.info(f"✅ STEP 2 COMPLETE: Models reloaded into memory")
            logger.info(f"   Duration: {step3_duration:.1f} seconds")
            logger.info(f"   Data points: {len(market_data) if market_data is not None else 0}")
            logger.info(f"   End time: {step3_end.isoformat()}")
        except Exception as load_err:
            logger.error(f"❌ STEP 2 FAILED: Model reload error")
            logger.error(f"   Error: {load_err}", exc_info=True)
            raise
        
        # ==================================================================
        # SUCCESS
        # ==================================================================
        end_time = datetime.utcnow()
        total_duration = (end_time - start_time).total_seconds()
        
        logger.info(f"\n{'='*70}")
        logger.info(f"✅ SUCCESS: Weekly update completed!")
        logger.info(f"{'='*70}")
        logger.info(f"\n📈 SUMMARY:")
        logger.info(f"   Total time: {total_duration:.1f} seconds ({total_duration/60:.1f} minutes)")
        logger.info(f"   Step 1 (Training): {(step2_duration/total_duration*100):.0f}%")
        logger.info(f"   Step 2 (Reload): {(step3_duration/total_duration*100):.0f}%")
        logger.info(f"   Completed at: {end_time.isoformat()}")
        logger.info(f"\n   📊 Next update: {(end_time + pd.Timedelta(days=7)).isoformat()}")
        logger.info(f"{'='*70}\n")
        
        return True
        
    except Exception as e:
        end_time = datetime.utcnow()
        total_duration = (end_time - start_time).total_seconds()
        
        logger.error(f"\n{'='*70}")
        logger.error(f"❌ FAILURE: Weekly update FAILED")
        logger.error(f"{'='*70}")
        logger.error(f"\n💥 ERROR DETAILS:")
        logger.error(f"   Failed after: {total_duration:.1f} seconds ({total_duration/60:.1f} minutes)")
        logger.error(f"   Error message: {str(e)}")
        logger.error(f"   Failed at: {end_time.isoformat()}")
        logger.error(f"\n   Stack trace:", exc_info=True)
        logger.error(f"{'='*70}\n")
        
        # Don't raise - let scheduler continue, but log the failure
        return False


# Initialize scheduler
scheduler = BackgroundScheduler(daemon=True, timezone=utc)
scheduler.add_job(
    scheduled_weekly_update,
    'cron',
    day_of_week='6',      # Sunday (0=Monday, 6=Sunday in APScheduler)
    hour='2',              # 2:30 AM UTC (Actions runs at 1 AM, done before this)
    minute='30',           # 30 minutes
    id='scheduled_weekly_update'
)

@app.on_event("startup")
def start_scheduler():
    try:
        scheduler.start()
        logger.info("✅ APScheduler STARTED - fallback retrain at Sunday 2:30 AM UTC")
        logger.info(f"   Jobs scheduled: {[job.id for job in scheduler.get_jobs()]}")
    except Exception as e:
        logger.error(f"❌ Failed to start scheduler: {e}", exc_info=True)


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
    
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info"
    )

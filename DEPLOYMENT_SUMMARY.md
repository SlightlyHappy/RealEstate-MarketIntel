# PropertyIntel on Railway - Complete Deployment Summary

## 🎯 What You're Deploying

A fully automated market intelligence system that:
1. **Every Sunday 2 AM UTC:** Scrapes 1,000+ new properties across 10 Indian cities
2. **Updates ML model** with latest prices and market trends
3. **Exposes API** for realpoint.in frontend to show price estimates, market heatmap, and deals
4. **Persists data** across deployments using Railway volumes

---

## 📊 System Flow

```
realpoint.in (Next.js)
    │
    └─→ /api/proxy/estimate-price
            │
            └─→ Railway FastAPI (8000)
                    ├─→ load_models() [from /data/models]
                    ├─→ predict(bhk, area, location)
                    └─→ return {"price_cr": 4.50, ...}


Every 7 Days (Sunday 2 AM UTC):
    
    Railway Scheduler
        ├─→ scrape_infinite_parallel()
        │   └─→ 10 cities × 15 pages = 1,000+ properties
        │       └─→ save to /data/raw/magicbricks_all_cities.jsonl
        │
        ├─→ retrain_model()
        │   └─→ read /data/raw/magicbricks_all_cities.jsonl
        │       └─→ train Random Forest (94.6% accurate)
        │           └─→ save to /data/models/price_predictor_rf.pkl
        │
        └─→ reload_models()
            └─→ API reloads models (users don't see downtime)
```

---

## 🚀 Deployment Timeline

### Day 0: Prep (5 minutes)
```
✅ models/price_predictor_rf.pkl → Already trained, saved locally
✅ models/encoders.pkl → Ready
✅ data/raw/magicbricks_all_cities.jsonl → 1,378 properties loaded
✅ src/railway/main.py → FastAPI server written
✅ Dockerfile → Build instructions ready
✅ requirements.txt → All deps listed (FastAPI, APScheduler, etc)
```

### Day 1: Deploy (10 minutes)
```
1. Push to GitHub
   git add .
   git commit -m "Railway deployment"
   git push origin main

2. Railway auto-detects Dockerfile
   Builds image (1 min)
   Deploys (1 min)
   ✅ Server running at https://your-app.up.railway.app

3. Create volume /data
   Railway dashboard → Settings → Add Volume
   
4. Copy models + data to Railway
   scp models/* railway:/data/models/
   scp data/raw/* railway:/data/raw/
```

### Day 7: First Automated Update (happens automatically)
```
Sunday 2 AM UTC:
  └─→ APScheduler triggers scheduled_weekly_update()
      ├─→ Scrapes, trains model, reloads (total: ~20 minutes)
      └─→ realpoint.in users see fresh data by 2:20 AM
```

### Every 7 Days: Automatic Updates
```
No manual work needed! Railway scheduler handles everything.
Logs visible in Railway dashboard.
```

---

## 🔌 realpoint.in Integration (Next.js)

### Environment Setup
```env
# .env.local
RAILWAY_API_URL=https://your-app.up.railway.app
```

### API Integration File: `pages/api/proxy.ts`
```typescript
export default async function handler(req, res) {
  const { endpoint, ...params } = req.query;
  
  const url = new URL(
    `${process.env.RAILWAY_API_URL}/api/${endpoint}`
  );
  Object.entries(params).forEach(([k, v]) => 
    url.searchParams.append(k, v)
  );
  
  const response = await fetch(url.toString());
  res.setHeader('Cache-Control', 'max-age=3600');
  res.json(await response.json());
}
```

### Dashboard Page: `pages/market-intelligence.tsx`
```typescript
export default function MarketDash() {
  const [estimate, setEstimate] = useState(null);
  const [heatmap, setHeatmap] = useState(null);
  
  // Fetch from API via proxy
  const estimatePrice = async (bhk, area, loc) => {
    const res = await fetch(
      `/api/proxy?endpoint=estimate-price&bhk=${bhk}&area_sqft=${area}&location=${loc}`
    );
    setEstimate(await res.json());
  };
  
  useEffect(() => {
    fetch(`/api/proxy?endpoint=market-heatmap`)
      .then(r => r.json())
      .then(setHeatmap);
  }, []);
  
  return (
    <div className="dashboard">
      {/* Price Estimator Widget */}
      {/* Market Heatmap Table */}
      {/* Deals Cards */}
    </div>
  );
}
```

---

## 📁 File Structure After Deployment

### On Your Local Machine (before deployment)
```
PropertyPredicitons/
├── data/
│   └── raw/
│       ├── magicbricks_all_cities.jsonl    [push to Railway]
│       └── magicbricks_all_cities.csv
├── models/
│   ├── price_predictor_rf.pkl              [push to Railway]
│   └── encoders.pkl                        [push to Railway]
├── src/
│   ├── railway/
│   │   └── main.py                         [FastAPI server]
│   ├── scrapers/
│   │   └── magicbricks_scraper.py          [Called by scheduler]
│   ├── api/
│   │   └── app.py                          [Old Flask version - not used]
├── Dockerfile                               [Railway uses this]
├── requirements.txt                         [Railway uses this]
└── README.md
```

### On Railway Persistent Volume (/data)
```
/data/
├── raw/
│   ├── magicbricks_all_cities.jsonl        [Updated every 7 days]
│   └── magicbricks_all_cities.csv
├── models/
│   ├── price_predictor_rf.pkl              [Retrained every 7 days]
│   └── encoders.pkl
└── logs/
    ├── scheduler.log                        [Weekly execution logs]
    └── api.log                              [Request logs]
```

---

## 🔒 Security & Admin

### API Key for Manual Triggers
```bash
# In Railway environment variables
ADMIN_API_KEY=your-super-secret-key-12345

# To manually trigger scraper (if needed)
curl -X POST https://your-app.up.railway.app/admin/trigger-scraper \
  -H "X-API-Key: your-super-secret-key-12345"
```

### CORS Configured For
- realpoint.in
- *.realpoint.in
- localhost (for testing)

### Rate Limiting (optional upgrade)
```python
from slowapi import Limiter
app.state.limiter = Limiter(key_func=get_remote_address)

@app.get("/api/estimate-price")
@limiter.limit("100/minute")
def estimate_price(...):
    ...
```

---

## 📊 API Endpoints realpoint.in Will Use

### 1. Price Estimator
```
GET /api/estimate-price?bhk=3&area_sqft=1500&location=Mumbai

Response:
{
  "property": {"bhk": 3, "area_sqft": 1500, "location": "Mumbai"},
  "estimate": {"price_cr": 4.50, "price_per_sqft": 30000},
  "confidence": {"accuracy": 0.946}
}
```

### 2. Market Heatmap
```
GET /api/market-heatmap

Response:
{
  "heatmap": [
    {"location": "Mumbai", "avg_price_cr": 3.52, "price_per_sqft": 34926, "status": "🔥 HOT"},
    {"location": "Bangalore", "avg_price_cr": 2.27, "price_per_sqft": 14433, "status": "🔥 HOT"},
    ...
  ]
}
```

### 3. Deals This Week
```
GET /api/deals-this-week?min_discount=15

Response:
{
  "deals": [
    {
      "location": "Greater Noida",
      "bhk": 3,
      "area_sqft": 1551,
      "listed_price_cr": 0.45,
      "fair_value_cr": 0.66,
      "discount_pct": 31.6
    }
  ],
  "count": 15
}
```

### 4. Market Insights
```
GET /api/market-insights

Response:
{
  "market": {
    "total_properties": 1378,
    "avg_price_cr": 2.75,
    "price_range": {"min_cr": 0.03, "max_cr": 72.00}
  },
  "model": {"accuracy_r2": 0.946},
  "data": {"last_updated": "2026-02-26T02:20:00", "next_update": "2026-03-05T02:00:00"}
}
```

---

## 🎨 How It Looks on realpoint.in

### Dashboard Layout
```
╔════════════════════════════════════════════════════════════════╗
║           Market Intelligence - Powered by PropertyIntel       ║
╠════════════════════════════════════════════════════════════════╣
║                                                                ║
║  PRICE ESTIMATOR                                              ║
║  ┌─────────────────────────────────────────────────────────┐ ║
║  │ BHK: [3]  Area: [1500] sqft  Location: [Mumbai ▼]       │ ║
║  │ [Estimate Price]                                         │ ║
║  │ Fair Market Price: ₹4.50 Cr (₹30,000/sqft)              │ ║
║  │ Confidence: 94.6% | Last updated: Feb 26, 2:20 AM       │ ║
║  └─────────────────────────────────────────────────────────┘ ║
║                                                                ║
║  MARKET HEATMAP (Location Rankings)                           ║
║  ┌─────────────────────────────────────────────────────────┐ ║
║  │ Location      │ Avg Price/sqft  │ Avg Price │ Status   │ ║
║  ├─────────────────────────────────────────────────────────┤ ║
║  │ 🔥 Mumbai     │ ₹34,926/sqft    │ ₹3.52 Cr  │ HOT      │ ║
║  │ 🌤️ Bangalore  │ ₹14,433/sqft    │ ₹2.27 Cr  │ WARM     │ ║
║  │ 🌤️ New Delhi  │ ₹17,046/sqft    │ ₹2.48 Cr  │ WARM     │ ║
║  └─────────────────────────────────────────────────────────┘ ║
║                                                                ║
║  DEALS THIS WEEK (15%+ Discount)                              ║
║  ┌─────────────────────────────────────────────────────────┐ ║
║  │ Greater Noida - 3 BHK, 1551 sqft                         │ ║
║  │ Listed: ₹0.45 Cr | Fair: ₹0.66 Cr                       │ ║
║  │ 💎 SAVE ₹20.8 Lakhs (31.6% discount)      [View]        │ ║
║  │                                                           │ ║
║  │ Mumbai - 2 BHK, 800 sqft                                 │ ║
║  │ Listed: ₹8.50 Cr | Fair: ₹11.20 Cr                      │ ║
║  │ 💎 SAVE ₹27 Lakhs (24.1% discount)        [View]        │ ║
║  └─────────────────────────────────────────────────────────┘ ║
║                                                                ║
╚════════════════════════════════════════════════════════════════╝
```

---

## ⏰ Weekly Automated Execution Timeline

```
Sunday 2:00 AM UTC  → Scheduler wakes up
                2:01 → Scraper starts (fetches 10 cities)
                2:15 → ML retraining begins (uses fresh data)
                2:25 → Models saved to /data/models/
                2:26 → API reloads models
                2:27 → ✅ Complete! realpoint.in has fresh data

Notes:
- Users see NO downtime during this process
- Logs show in Railway dashboard
- Continues even if Railway server restarts (scheduler is persistent)
```

---

## 📞 Support & Troubleshooting

### Common Issues

**Issue:** "Models loading" error
```
Cause: First deployment, models haven't loaded yet
Fix: Wait 2 minutes, try again
```

**Issue:** API slow to respond
```
Cause: Model prediction takes time for first request
Fix: Response is cached for 1 hour on Next.js side
```

**Issue:** Data not updating
```
Cause: Scheduler might have failed
Fix: Check Railway logs, manually trigger `/admin/trigger-scraper`
```

**Issue:** CORS errors from realpoint.in
```
Cause: Incorrect domain in CORS config
Fix: Update CORS_ORIGINS in Railway env variables
```

---

## 🎯 What You Have Now

✅ Production-grade ML API (94.6% accuracy)  
✅ Automated weekly data refresh  
✅ Zero-downtime model updates  
✅ Persistent data storage on Railway  
✅ Fully configured for realpoint.in integration  
✅ Admin endpoints for manual triggers  
✅ Health monitoring and logging  

---

## Next Steps

1. **Push this code to GitHub**
2. **Create Railway account**
3. **Connect GitHub repo to Railway**
4. **Set environment variables**
5. **Add /data volume**
6. **Deploy (auto-deploys on git push)**
7. **Test APIs**
8. **Integrate with realpoint.in**

**Time to production:** ~30 minutes ⚡

---

**Questions?** Check RAILWAY_QUICKSTART.md or RAILWAY.md for detailed setup!

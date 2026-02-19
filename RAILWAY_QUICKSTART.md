# Railway Deployment - Quick Start Guide

## Step 1: Connect GitHub to Railway

1. Go to https://railway.app
2. Sign up / Login
3. Create new project → Deploy from GitHub
4. Select this repository

## Step 2: Configure Railway

```bash
# In Railway dashboard, set environment variables:

DATA_DIR=/data/raw
MODEL_DIR=/data/models
LOG_DIR=/data/logs
ADMIN_API_KEY=your-secret-key-here
RAILWAY_VOLUME_MOUNT_PATH=/data
```

## Step 3: Add Persistent Volume

In Railway dashboard:
1. Click the service → Settings
2. Find "Volumes" section
3. Add volume: Mount path = `/data`

## Step 4: Deploy

Railway auto-deploys on git push:
```bash
git add .
git commit -m "Update for Railway deployment"
git push origin main
```

## Step 5: Test API

Once deployed, you'll get a Railway URL like:
```
https://your-app.up.railway.app
```

Test it:
```bash
curl https://your-app.up.railway.app/health
curl "https://your-app.up.railway.app/api/estimate-price?bhk=3&area_sqft=1500&location=Mumbai"
```

## Step 6: Add to realpoint.in

In your Next.js environment variables:
```
RAILWAY_API_URL=https://your-app.up.railway.app
```

## Monitoring

View logs in Railway dashboard:
```bash
# Latest logs show scheduler running weekly
[2026-02-26 02:00:00] Starting weekly market intelligence update
[2026-02-26 02:15:00] ✅ Weekly update successful!
```

## Manual Trigger (if needed)

```bash
curl -X POST https://your-app.up.railway.app/admin/trigger-scraper \
  -H "X-API-Key: your-secret-key-here"
```

## What Happens Automatically

- **Every 7 days at 2 AM UTC:**
  1. Scraper runs and collects new properties from Magic Bricks
  2. ML model retrains on latest data
  3. API reloads models (zero downtime)
  4. realpoint.in dashboard shows fresh data

- **Data persists** to the `/data` volume across deployments
- **Logs saved** to `/data/logs/` for troubleshooting

## Cost Estimate (on Railway)

- FastAPI server: ~$5/month (shared tier)
- 5GB data volume: ~$5/month
- Total: ~$10/month

## Troubleshooting

1. **API returns "Models loading"**
   - First deployment takes ~2 minutes to load models
   - Wait 2 minutes and try again

2. **Data not updating**
   - Check Railway logs for scheduler errors
   - Manually trigger: `POST /admin/trigger-scraper`

3. **Volume not persisting**
   - Verify volume is mounted at `/data`
   - Check Railway dashboard under Volumes

---

**Next:** Configure realpoint.in frontend to call this API!

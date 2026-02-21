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

## Monitoring Scheduler

The system runs **every Sunday 2 AM UTC** automatically. To check its status:

```bash
# Check if scheduler is running and when next job fires
curl -H "X-API-Key: your-secret-key-here" \
  https://your-app.up.railway.app/admin/scheduler-status

# Response shows next_run_time (should be next Sunday 2 AM UTC)
```

## Testing the Scheduler

Don't want to wait for Sunday? Test immediately:

```bash
# Manually run the weekly update (scrape + retrain + reload)
curl -X POST \
  -H "X-API-Key: your-secret-key-here" \
  https://your-app.up.railway.app/admin/test-scheduler

# This runs the ENTIRE weekly pipeline immediately
# Completes in ~2-5 minutes
# Check Railway logs for progress
```

## Manual Trigger (if needed)

```bash
curl -X POST https://your-app.up.railway.app/admin/trigger-scraper \
  -H "X-API-Key: your-secret-key-here"
```

## What Happens Automatically

- **Every Sunday 2 AM UTC:**
  1. Scraper runs and collects new properties from Magic Bricks
  2. ML model retrains on latest data
  3. API reloads models (zero downtime)
  4. realpoint.in dashboard shows fresh data

- **Data persists** to the `/data` volume across deployments
- **Logs saved** to `/data/logs/` for troubleshooting

## Expected Log Output

When the scheduler runs ( automatically on Sunday or via `/admin/test-scheduler`):

```
======================================================================
🔄 [SCHEDULED TASK TRIGGERED] 2026-02-22T02:00:00.000000Z
======================================================================
STEP 1/3: Running property scraper...
✅ STEP 1 COMPLETE: Scraper completed successfully
STEP 2/3: Retraining ML model...
✅ STEP 2 COMPLETE: Model retraining successful
STEP 3/3: Reloading models into memory...
✅ STEP 3 COMPLETE: Models reloaded
======================================================================
✅ SUCCESS: Weekly update completed in 142.5s
======================================================================
```

## Cost Estimate (on Railway)

- FastAPI server: ~$5/month (shared tier)
- 5GB data volume: ~$5/month
- Total: ~$10/month

## Troubleshooting

1. **API returns "Models loading"**
   - First deployment takes ~2 minutes to load models
   - Wait 2 minutes and try again

2. **Data not updating**
   - Check scheduler status: `GET /admin/scheduler-status`
   - Next run time should show next Sunday 2 AM UTC
   - To test immediately: `POST /admin/test-scheduler`
   - Check Railway logs for any errors (look for FAILURE)

3. **Scheduler shows as "Stopped"**
   - Restart Railway deployment
   - Check logs for startup errors
   - Ensure `pytz` is in requirements.txt

4. **Volume not persisting**
   - Verify volume is mounted at `/data`
   - Check Railway dashboard under Volumes

5. **For detailed troubleshooting**
   - See [SCHEDULER_DEBUGGING.md](SCHEDULER_DEBUGGING.md)
   - See [SCHEDULER_FIX_SUMMARY.md](SCHEDULER_FIX_SUMMARY.md)

---

**Next:** Configure realpoint.in frontend to call this API!

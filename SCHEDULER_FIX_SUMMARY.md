# Sunday Scheduler Fix - Summary

## What Was Wrong

The scheduled weekly update (every Sunday 2 AM UTC) was not executing on Railway. No scraping logs appeared in the Railway logs, meaning the job never ran or it failed silently.

## Root Causes Found & Fixed

### 1. **🔴 Timezone Bug**
- **Problem:** APScheduler was using system timezone by default, not UTC
- **Fix:** Explicitly set `timezone=utc` when initializing scheduler
- **File:** `src/railway/main.py` line 778

### 2. **🔴 Wrong Day of Week**  
- **Problem:** Used `day_of_week='0'` which is Monday, not Sunday
- **Fix:** Changed to `day_of_week='6'` (Sunday in APScheduler: 0=Mon, 6=Sun)
- **File:** `src/railway/main.py` line 782

### 3. **🔴 Silent Exceptions**
- **Problem:** Errors in scheduled_weekly_update() were logged but not visible in Railway logs
- **Fix:** Added comprehensive error logging with timestamps, stack traces, and formatted output
- **File:** `src/railway/main.py` lines 667-731

### 4. **🔴 Missing Dependency**
- **Problem:** `pytz` needed for explicit UTC timezone handling
- **Fix:** Added `pytz==2024.1` to requirements.txt
- **File:** `requirements.txt` line 9

## New Debugging Features Added

### `/admin/scheduler-status` (GET)
Check if scheduler is running and when next job will execute
```bash
curl -H "X-API-Key: YOUR_ADMIN_KEY" \
  https://your-app.railway.app/admin/scheduler-status
```

Returns:
```json
{
  "status": "Running",  // ← Should be "Running"
  "jobs": [{
    "id": "scheduled_weekly_update",
    "trigger": "cron[day_of_week = '6', hour = '2', minute = '0']",
    "next_run_time": "2026-02-22T02:00:00+00:00",  // ← Next Sunday 2 AM UTC
    "timezone": "UTC"  // ← Should be "UTC"
  }],
  "server_time": "2026-02-21T06:38:43.643123456Z",
  "server_timezone": "UTC"
}
```

### `/admin/test-scheduler` (POST)
Manually run the entire weekly update immediately (for testing)
```bash
curl -X POST -H "X-API-Key: YOUR_ADMIN_KEY" \
  https://your-app.railway.app/admin/test-scheduler
```

This runs: scrape → retrain → reload (same as Sunday 2 AM)

## Files Changed

1. **src/railway/main.py**
   - Added `from pytz import utc` import
   - Fixed scheduler timezone: `timezone=utc`
   - Fixed cron day_of_week: `'6'` (Sunday)
   - Rewrote `scheduled_weekly_update()` with better logging
   - Added `/admin/test-scheduler` endpoint
   - Added `/admin/scheduler-status` endpoint
   - Improved startup logging

2. **requirements.txt**
   - Added `pytz==2024.1`

3. **SCHEDULER_DEBUGGING.md** (NEW)
   - Complete debugging guide
   - Test procedures
   - Common issues and solutions

## How to Verify the Fix

### Step 1: Deploy Changes
Push the code to GitHub - Railway will auto-deploy

### Step 2: Check Scheduler is Running
```bash
curl -H "X-API-Key: YOUR_KEY" https://your-app.railway.app/admin/scheduler-status
```
✅ Should show `"status": "Running"` and correct next_run_time

### Step 3: Test Immediately (Don't Wait for Sunday)
```bash
curl -X POST -H "X-API-Key: YOUR_KEY" https://your-app.railway.app/admin/test-scheduler
```
✅ Should complete in ~2-5 minutes
✅ Railway logs should show scraping + model training + reload

### Step 4: Monitor Next Sunday 2 AM UTC
- Watch Railway logs for the automatic job execution
- Should see same output as test-scheduler
- Website data should update after completion

## Expected Log Output When Job Runs

```
======================================================================
🔄 [SCHEDULED TASK TRIGGERED] 2026-02-22T02:00:00.000000Z
======================================================================
STEP 1/3: Running property scraper...
  - Scraper path: /app/src/scrapers
  - Output directory: /data/raw
  - 10 cities, 15 pages each (~1000 properties)
✅ STEP 1 COMPLETE: Scraper completed successfully

STEP 2/3: Retraining ML model...
  - 950 cleaned properties
  - Training Random Forest...
  - Model Performance - MAE: 0.0954 Cr, R²: 0.9460
✅ STEP 2 COMPLETE: Model retraining successful

STEP 3/3: Reloading models into memory...
  - 950 properties loaded
✅ STEP 3 COMPLETE: Models reloaded

======================================================================
✅ SUCCESS: Weekly update completed in 142.5s
   Completed at: 2026-02-22T02:02:22.500000Z
======================================================================
```

If `FAILURE` appears instead, the error message will show what went wrong.

## Deployment Checklist

- [x] Fixed timezone to UTC
- [x] Fixed day_of_week to Sunday (value 6)
- [x] Added comprehensive error logging  
- [x] Added pytz dependency
- [x] Added `/admin/scheduler-status` endpoint for monitoring
- [x] Added `/admin/test-scheduler` endpoint for manual testing
- [x] Verified no syntax errors
- [x] Created debugging documentation

## Next Steps

1. Commit and push changes
2. Wait for Railway deployment to complete (~2-3 minutes)
3. Test using `/admin/test-scheduler` endpoint
4. If successful, next Sunday 2 AM UTC should auto-run
5. Refer to SCHEDULER_DEBUGGING.md if any issues occur

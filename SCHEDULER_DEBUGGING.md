# Scheduler Debugging Guide

## Problem Statement
Sunday scheduled updates (2 AM UTC) are not executing. No scraping logs visible in Railway.

## Root Causes Identified & Fixed

### 1. **Timezone Issue** ✅ FIXED
**Problem:** APScheduler was using system timezone (default), not UTC
**Solution:** Explicitly set `timezone=utc` in `BackgroundScheduler()`
```python
scheduler = BackgroundScheduler(daemon=True, timezone=utc)
```

### 2. **Wrong Day of Week** ✅ FIXED  
**Problem:** `day_of_week='0'` is Monday, not Sunday
**Solution:** Changed to `day_of_week='6'` (0=Monday, 6=Sunday)
```python
scheduler.add_job(
    scheduled_weekly_update,
    'cron',
    day_of_week='6',  # Sunday
    hour='2',          # 2 AM
    minute='0',
    id='scheduled_weekly_update'
)
```

### 3. **Silent Exception Handling** ✅ FIXED
**Problem:** Exceptions in `scheduled_weekly_update()` were caught but not visible
**Solution:** Added comprehensive error logging with timestamps and stack traces
```python
except Exception as e:
    logger.error(f"❌ FAILURE: Weekly update FAILED after {duration:.1f}s")
    logger.error(f"   Error: {str(e)}")
    logger.error(f"   Failed at: {end_time.isoformat()}")
    logger.error("="*70 + "\n", exc_info=True)  # This includes stack trace
```

### 4. **Missing pytz Dependency** ✅ FIXED
**Problem:** Timezone handling might fail without pytz
**Solution:** Added `pytz==2024.1` to requirements.txt

---

## How to Test & Verify

### Test 1: Check Scheduler Status
```bash
curl -H "X-API-Key: <ADMIN_API_KEY>" \
  https://your-railway-domain.com/admin/scheduler-status
```

Expected response:
```json
{
  "status": "Running",
  "jobs": [
    {
      "id": "scheduled_weekly_update",
      "func": "scheduled_weekly_update",
      "trigger": "cron[day_of_week = '6', hour = '2', minute = '0']",
      "next_run_time": "2026-02-21T02:00:00+00:00",
      "timezone": "UTC"
    }
  ],
  "server_time": "2026-02-21T06:38:43.643123456Z",
  "server_timezone": "UTC"
}
```

**Key checks:**
- ✅ `"status": "Running"` - Scheduler is active
- ✅ `"timezone": "UTC"` - Using correct timezone
- ✅ `next_run_time` - Should be next Sunday 2 AM UTC
- ✅ Trigger shows correct day (6) and hour (2)

### Test 2: Manually Run Scheduler Job
```bash
curl -X POST \
  -H "X-API-Key: <ADMIN_API_KEY>" \
  https://your-railway-domain.com/admin/test-scheduler
```

**What this does:** Runs the complete weekly update immediately (scrape + retrain + reload)

**Expected response:**
```json
{
  "status": "Success",
  "message": "Test scheduler run completed successfully",
  "timestamp": "2026-02-21T06:40:12.345678Z",
  "next_scheduled": "Sunday 2 AM UTC"
}
```

**In Railway logs, you should see:**
```
======================================================================
🔄 [SCHEDULED TASK TRIGGERED] 2026-02-21T06:40:12.345678Z
======================================================================
STEP 1/3: Running property scraper...
  - Scraper path: /app/src/scrapers
  - Output directory: /data/raw
...
✅ STEP 1 COMPLETE: Scraper completed successfully
STEP 2/3: Retraining ML model...
...
✅ SUCCESS: Weekly update completed in 120.5s
======================================================================
```

### Test 3: Check Specific Job Details
```bash
curl -H "X-API-Key: <ADMIN_API_KEY>" \
  https://your-railway-domain.com/admin/scheduler-status | jq '.jobs[0]'
```

### Test 4: Monitor Railway Logs
Filter for scheduler messages:
```bash
# In Railway logs, search for:
"SCHEDULED TASK TRIGGERED"  # Means job started
"STEP 1/3"                  # Scraper running
"STEP 2/3"                  # Model training
"STEP 3/3"                  # Model reloading
"SUCCESS"                   # Job completed
"FAILURE"                   # Job failed (will show error message)
```

---

## Common Issues & Solutions

### Issue: `"status": "Stopped"` in scheduler-status
**Cause:** Scheduler failed to start during app initialization
**Solution:** 
1. Check Railway app logs during startup
2. Look for "Failed to start scheduler" errors
3. Check that `pytz` is in requirements.txt
4. Restart the Railway deployment

### Issue: Correct next_run_time but job never executes
**Cause:** Exception is happening silently somewhere
**Solution:**
1. Run `/admin/test-scheduler` to test immediately
2. Check the response - if it fails, there's an error in scraver/model code
3. Look at the detailed error message returned
4. Check Railway logs under `/data/logs/` directory

### Issue: next_run_time is NULL or wrong date
**Cause:** 
- APScheduler expression parsing error
- Timezone mismatch
**Solution:**
1. Verify `day_of_week='6'` in code
2. Verify `timezone=utc` is set
3. Restart Railway deployment

### Issue: Scraper times out (> 10 minutes)
**Cause:** Railway might timeout long-running processes
**Solution:**
1. Reduce `max_pages` parameter in scheduled job (currently 15)
2. Reduce `max_workers` parameter (currently 2)
3. Add timeout handling in scraper

---

## Deployment Steps (After Code Changes)

1. Push changes to GitHub
2. Railway auto-deploys
3. Wait for deployment to complete (~2-3 minutes)
4. Test `/admin/scheduler-status` endpoint  
5. If Sunday hasn't arrived, run `/admin/test-scheduler` to test immediately
6. Monitor logs for any errors

---

## Important Environment Variables

Make sure these are set in Railway:

```env
ADMIN_API_KEY=<your-secret-key>
PORT=8000
DATA_DIR=/data/raw
MODEL_DIR=/data/models  
LOG_DIR=/data/logs
```

---

## Next Run Time Calculations

If it's **Thursday Feb 20, 2026 at 14:38 UTC:**
- Next Sunday 2 AM UTC = **Feb 22, 2026 at 02:00 UTC**
- That's in 35 hours

If it's **Friday Feb 21, 2026:**
- Next Sunday 2 AM UTC = **Feb 22, 2026 at 02:00 UTC**
- That's in ~... hours

---

## Logs Location

After an update runs, check logs at:
- Railway UI > Logs tab (real-time)
- `/data/logs/` directory (persistent logs, if configured)

Look for timestamps matching the scheduled time.

---

## Summary Checklist

- [x] Fixed timezone to UTC
- [x] Fixed day_of_week to Sunday (6)  
- [x] Added comprehensive error logging
- [x] Added pytz dependency
- [x] Added `/admin/scheduler-status` endpoint
- [x] Added `/admin/test-scheduler` endpoint
- [x] Improved startup logging
-[x] Better exception handling with stack traces

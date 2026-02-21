# 🔧 PropertyIntel Sunday Scheduler - Complete Fix

## Problem Summary
The scheduled weekly update (Sunday 2 AM UTC) to scrape property data and retrain the ML model was not executing on Railway. No scraping occurred, and no logs showed the job running.

---

## Root Cause Analysis

### Bug #1: Wrong Timezone Configuration ❌
**Location:** `src/railway/main.py` line 778  
**Problem:** APScheduler was using system timezone (default), not UTC  
**Impact:** Job never would fire at correct UTC time  
**Fix:** 
```python
# BEFORE:
scheduler = BackgroundScheduler(daemon=True)

# AFTER:
scheduler = BackgroundScheduler(daemon=True, timezone=utc)
```

### Bug #2: Incorrect Day of Week ❌
**Location:** `src/railway/main.py` line 782  
**Problem:** Used `day_of_week='0'` which is Monday in APScheduler  
**Impact:** Job never ran on Sunday (would run on Monday instead, if at all)  
**Fix:**
```python
# BEFORE:
day_of_week='0',      # Assumed 0 = Sunday (WRONG!)

# AFTER:
day_of_week='6',      # Sunday in APScheduler (0=Mon, 6=Sun)
```

### Bug #3: Silent Exception Handling ❌
**Location:** `src/railway/main.py` lines 667-731  
**Problem:** Exceptions in scheduled jobs were caught but not logged visibly  
**Impact:** If scraper or model failed, error was hidden - just silent failure  
**Fix:** Added comprehensive logging with:
- Step-by-step progress messages
- Timestamps for each step
- Full stack traces on errors
- Duration tracking for performance monitoring

### Bug #4: Missing Timezone Dependency ❌
**Location:** `requirements.txt`  
**Problem:** `pytz` library not included but needed for UTC timezone handling  
**Impact:** Timezone configuration might fail silently  
**Fix:** Added `pytz==2024.1` to dependencies

---

## Files Modified

### 1. `src/railway/main.py`
**Changes:**
- ✅ Added `from pytz import utc` import (line 10)
- ✅ Changed scheduler initialization to use UTC (line 778)
- ✅ Fixed day_of_week from '0' to '6' (line 782)  
- ✅ Rewrote `scheduled_weekly_update()` with better logging (lines 667-731)
- ✅ Added `/admin/test-scheduler` endpoint (lines 627-660)
- ✅ Added `/admin/scheduler-status` endpoint (lines 661-695)
- ✅ Improved startup event logging (lines 255-273)

**Key Improvements:**
```python
# Better error logging example:
logger.error(f"{'='*70}")
logger.error(f"❌ FAILURE: Weekly update FAILED after {duration:.1f}s")
logger.error(f"   Error: {str(e)}")
logger.error(f"   Failed at: {end_time.isoformat()}")
logger.error("="*70 + "\n", exc_info=True)  # Stack trace

# Better scheduler setup:
scheduler = BackgroundScheduler(daemon=True, timezone=utc)
scheduler.add_job(
    scheduled_weekly_update,
    'cron',
    day_of_week='6',  # Sunday  
    hour='2',         # 2 AM UTC
    minute='0',       # 00 minutes
    id='scheduled_weekly_update'
)
```

### 2. `requirements.txt`
**Added:**
```
pytz==2024.1
```
(Moved from line 8 to line 9 to add timezone support)

### 3. `SCHEDULER_FIX_SUMMARY.md` (NEW)
Quick reference guide explaining:
- What was wrong
- How it was fixed
- How to verify the fix
- How to test manually

### 4. `SCHEDULER_DEBUGGING.md` (NEW)  
Comprehensive troubleshooting guide with:
- Root cause analysis for each bug
- How to test each component
- Common issues and solutions
- Log monitoring procedures
- Environment variable reference

### 5. `RAILWAY_QUICKSTART.md` (UPDATED)
Added sections:
- How to check scheduler status
- How to test scheduler immediately
- Expected log output
- Enhanced troubleshooting for scheduler issues

---

## New Admin Endpoints

### Endpoint 1: Check Scheduler Status
```bash
curl -H "X-API-Key: YOUR_ADMIN_KEY" \
  https://your-app.railway.app/admin/scheduler-status
```

**Response:**
```json
{
  "status": "Running",
  "jobs": [{
    "id": "scheduled_weekly_update",
    "func": "scheduled_weekly_update",
    "trigger": "cron[day_of_week = '6', hour = '2', minute = '0']",
    "next_run_time": "2026-02-22T02:00:00+00:00",
    "timezone": "UTC"
  }],
  "server_time": "2026-02-21T06:38:43Z",
  "server_timezone": "UTC"
}
```

**What to look for:**
- ✅ `"status": "Running"` - scheduler is active
- ✅ `"timezone": "UTC"` - using correct timezone
- ✅ `next_run_time` - next Sunday 2 AM UTC
- ✅ `day_of_week = '6'` - correct day (Sunday)

### Endpoint 2: Test Scheduler Immediately
```bash
curl -X POST \
  -H "X-API-Key: YOUR_ADMIN_KEY" \
  https://your-app.railway.app/admin/test-scheduler
```

**What it does:** Runs the complete weekly update cycle immediately:
1. Scrapes MagicBricks (~15 min)
2. Retrains ML model (~3 min)
3. Reloads models (~1 min)

**Expected output in Railway logs:**
```
======================================================================
🔄 [SCHEDULED TASK TRIGGERED] 2026-02-21T06:40:00.000000Z  
======================================================================
STEP 1/3: Running property scraper...
  - Scraper path: /app/src/scrapers
  - Output directory: /data/raw
✅ STEP 1 COMPLETE: Scraper completed successfully

STEP 2/3: Retraining ML model...
  - 950 cleaned properties
  - Training Random Forest...
✅ STEP 2 COMPLETE: Model retraining successful

STEP 3/3: Reloading models into memory...
✅ STEP 3 COMPLETE: Models reloaded

======================================================================
✅ SUCCESS: Weekly update completed in 142.5s
   Completed at: 2026-02-21T06:42:00.000000Z
======================================================================
```

---

## How to Deploy & Test

### Step 1: Commit and Push
```bash
cd /path/to/PropertyPredictions
git add -A
git commit -m "Fix: Scheduler timezone and day-of-week bugs"
git push origin main
```

### Step 2: Wait for Railway Deployment
- Go to Railway dashboard
- Watch for deployment to complete (~2-3 minutes)
- Check the "Logs" tab for startup messages

### Step 3: Verify Scheduler is Running
```bash
# Check scheduler status
curl -H "X-API-Key: YOUR_ADMIN_KEY" \
  https://your-app.railway.app/admin/scheduler-status

# Should show:
# - Status: "Running"
# - Timezone: "UTC"  
# - Next run time: this Sunday 2 AM UTC
```

### Step 4: Test Immediately (Optional)
```bash
# Don't wait for Sunday - test right now
curl -X POST \
  -H "X-API-Key: YOUR_ADMIN_KEY" \
  https://your-app.railway.app/admin/test-scheduler

# Watch Railway logs for progress...
# Should complete in 20-30 minutes
```

### Step 5: Verify Success
After test completes, check:
- ✅ New properties scraped: `/api/market-insights` shows updated count
- ✅ Model retrained: `/api/status` shows recent `last_updated` timestamp
- ✅ No errors in logs: Search for "FAILURE" - should find none

### Step 6: Wait for Automatic Run
Next Sunday at 2 AM UTC, the automatic scheduled job will run:
- Watch Railway logs for scheduled execution
- Should see same output as test-scheduler
- Website data will automatically refresh

---

## Expected Behavior After Fix

### Before (Broken) 🔴
```
Sunday 2 AM UTC arrives...
[nothing happens]
Logs show: [no scheduler activity]
Website: [data stays stale]
```

### After (Fixed) ✅
```
Sunday 2 AM UTC arrives...
[job fires automatically]
Logs show:
  🔄 [SCHEDULED TASK TRIGGERED]
  STEP 1/3: Running property scraper...
  ✅ STEP 1 COMPLETE
  STEP 2/3: Retraining ML model...
  ✅ STEP 2 COMPLETE
  ✅ SUCCESS: Weekly update completed
Website: [fresh data available]
```

---

## Rollback Plan (if needed)

If something goes wrong:

```bash
# Revert to previous commit
git revert HEAD
git push origin main

# Railway auto-deploys
# Wait for deployment to complete
```

---

## Monitoring Checklist

**Daily (inspect once):**
- [ ] API working: `curl https://your-app.railway.app/health`
- [ ] Price estimation works: `/api/estimate-price?bhk=2&area_sqft=1500&location=Mumbai`

**Weekly (after Sunday 2 AM UTC):**
- [ ] Check scheduler status endpoint
- [ ] Look for scheduled task logs
- [ ] Verify new data appeared: `/api/market-insights`

**Before each release:**
- [ ] Run `/admin/test-scheduler` to verify pipeline works
- [ ] Check all error logs for "FAILURE" keywords
- [ ] Ensure `requirements.txt` has `pytz`

---

## Reference Links

- **Debugging Guide:** See [SCHEDULER_DEBUGGING.md](SCHEDULER_DEBUGGING.md)
- **Quick Reference:** See [SCHEDULER_FIX_SUMMARY.md](SCHEDULER_FIX_SUMMARY.md)
- **Deployment Guide:** See [RAILWAY_QUICKSTART.md](RAILWAY_QUICKSTART.md)
- **Full System Docs:** See [DEPLOYMENT_SUMMARY.md](DEPLOYMENT_SUMMARY.md)

---

## Summary of Changes

| Component | Before | After | Status |
|-----------|--------|-------|--------|
| **Timezone** | default (system) | `utc` ✅ |
| **Day of Week** | `'0'` (Monday) | `'6'` (Sunday) ✅ |
| **Error Logging** | Silent failures | Detailed with stack traces ✅ |
| **Dependencies** | Missing pytz | Added `pytz==2024.1` ✅ |
| **Testing** | Manual scraper only | Added `/admin/test-scheduler` ✅ |
| **Monitoring** | No visibility | Added `/admin/scheduler-status` ✅ |
| **Documentation** | None | 3 new guides added ✅ |

---

**Status:** ✅ Ready for deployment

**Timeline:** Deploy → 3 min → Test (optional) → Monday onwards (auto-run every Sunday)

# Auto Sports Reservation Checker for SJTU 🏸🎾🏊 

## Note 
- 该项目仅用于上海交通大学校内体育场馆的空余场地检测，目的是监控场地的可预约状态并发送通知提醒，使得空余场地资源能够被更有效地利用。
- **不提供任何预订（抢场地）功能，同时谴责任何利用该项目（以及任何程序）进行抢场地的行为。**
- 目前自动通知功能仅支持 macOS 系统，其他系统用户可自行扩展。
- **请不要分享或公开发布包含个人账号信息的配置文件（如 `.env`），以防jAccount账号被盗用。**
- 请不要频繁运行该脚本，以免对馆预约系统造成不必要的负担。
- 本项目仅供学习和研究使用，不得将其用于任何违反**上海交通大学相关规定**的用途，使用者需自行承担相关责任。

## Project Status 

**Overview**
- Local Playwright+Python tool that logs into SJTU sports portal, selects a venue/room/date, and records newly available slots.
- Runs headless by default, saves session data under `user_data`, and persists notified slots in `state.db` to avoid duplicate alerts.

**Prerequisites**
- macOS (other systems supported but desktop alerts use macOS `osascript` fallback).
- Python 3.11 with conda environment `your_env_name` (already created according to setup notes).
- Tesseract OCR (`brew install tesseract`) so the script can auto-fill captcha.

**Setup Steps**
- Activate the conda env: `conda activate <your_env_name>`.
- Install dependencies: `pip install -r requirements.txt`.
- Install Playwright browsers: `playwright install chromium`.
- Create `.env` from `.env.example` and fill in your credentials and venue/date filters.
- Run once in headful mode to complete any slider verification and store the session:
  `conda run -n <your_env_name> python autocheck_sports.py --once --headful`.

**Configuration**
- `USERNAME` / `PASSWORD`: SJTU jAccount login.
- `SEL_*`: CSS selectors for login elements; defaults match the current portal and generally require no changes.
- `SLOT_ITEM_SEL`: Defaults to `.unselected-seat` which fits the table layout for 台球/健身房类项目.
- `VENUE_KEYWORD` / `VENUE_SUB_KEYWORD`: Text used to click the main venue card and second-level tab (e.g. `学生中心` → `学生中心健身房`).
- `DATE_KEYWORD`: Optional date tab label such as `02月03日`.
- `CHECK_EVERY_MIN` / `RANDOM_JITTER_SEC`: Polling interval and jitter for the scheduler.
   - `ENABLE_DESKTOP`: Keep set to `1` for macOS notifications; non-macOS users may install `plyer` + platform backend manually.
- `TESSERACT_CMD`: Set to the absolute path of the Tesseract executable if it is not on `PATH`.

**Usage**
- Single check (headless): `conda run -n <your_env_name> python autocheck_sports.py --once`.
- Single check with visible browser for debugging: add `--headful`.
- Continuous monitoring: `conda run -n <your_env_name> python autocheck_sports.py` (respects the interval from `.env`).
- Logs are printed to stdout; redirect to `auto.log` if you need history: `... autocheck_sports.py >> auto.log 2>&1`.
- When you see "Discovered X new slots" in the logs, check your desktop notifications. This happens only when two conditions are met:
  1. New available slots are found that match your filters.
  2. These slots have not been notified before (deduped via `state.db`).

**Troubleshooting**
- Delete `state.db` if you want to clear the deduped slot history.
- macOS notifications fallback to `osascript`; if you prefer the old plyer backend, install `pyobjus` via `pip install pyobjus`.

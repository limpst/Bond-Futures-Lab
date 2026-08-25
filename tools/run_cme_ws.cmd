@echo off
REM ============================================================================
REM  CME 채권선물 1분봉 야간 수집 — Windows 작업 스케줄러용
REM
REM  CME 야간(아시아) 세션은 KST 18:00 ~ 익일 05:00 이다. 18:00 에 시작해
REM  660분(11시간) 동안 tick 을 받아 1분봉으로 적재한다.
REM
REM  등록 (관리자 권한 명령 프롬프트에서 한 번만):
REM    schtasks /create /tn "CME night bars" ^
REM      /tr "C:\Users\leeli\bond-futures-lab\tools\run_cme_ws.cmd" ^
REM      /sc weekly /d MON,TUE,WED,THU,FRI /st 18:00
REM
REM  해제:  schtasks /delete /tn "CME night bars" /f
REM  확인:  schtasks /query /tn "CME night bars"
REM
REM  로그: tools\logs\cme_ws_YYYYMMDD.log
REM  조회 전용 — 주문 TR 은 호출하지 않는다.
REM ============================================================================
setlocal
set "ROOT=%~dp0.."
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "MINUTES=660"

for /f "tokens=1-3 delims=/- " %%a in ('date /t') do set "TODAY=%%a%%b%%c"
set "LOGDIR=%ROOT%\tools\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "LOG=%LOGDIR%\cme_ws_%TODAY%.log"

echo ============================================ >> "%LOG%"
echo [%date% %time%] CME 야간 수집 시작 (%MINUTES%분) >> "%LOG%"

pushd "%ROOT%"
"C:\Users\leeli\AppData\Local\Programs\Python\Python312\python.exe" tools\collect_cme_ws.py --minutes %MINUTES% >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
popd

echo [%date% %time%] 종료 코드 %RC% >> "%LOG%"
if not "%RC%"=="0" exit /b %RC%
endlocal

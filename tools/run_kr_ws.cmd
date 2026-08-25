@echo off
REM ============================================================================
REM  KTB 선물 1분봉 주간 실시간 수집 — Windows 작업 스케줄러용
REM
REM  KRX 파생 주간 세션은 09:00~15:45. WebSocket(FC9) tick 은 이 시간에만 흐른다.
REM  (야간 18:00~05:00 은 REST t8461 폴링이 담당 — run_collect.cmd)
REM
REM  08:55 에 시작해 420분(=15:55) 까지 돈다. 연결이 끊기면 스스로 재접속한다.
REM
REM  등록:
REM    schtasks /create /tn "KTB day ticks" ^
REM      /tr "C:\Users\leeli\bond-futures-lab\tools\run_kr_ws.cmd" ^
REM      /sc weekly /d MON,TUE,WED,THU,FRI /st 08:55
REM
REM  로그: tools\logs\kr_ws_YYYYMMDD.log
REM  조회 전용 — 주문 TR 은 호출하지 않는다.
REM ============================================================================
setlocal
set "ROOT=%~dp0.."
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "MINUTES=420"

for /f "tokens=1-3 delims=/- " %%a in ('date /t') do set "TODAY=%%a%%b%%c"
set "LOGDIR=%ROOT%\tools\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "LOG=%LOGDIR%\kr_ws_%TODAY%.log"

echo ============================================ >> "%LOG%"
echo [%date% %time%] KTB 주간 실시간 수집 시작 (%MINUTES%분) >> "%LOG%"

pushd "%ROOT%"
"C:\Users\leeli\AppData\Local\Programs\Python\Python312\python.exe" tools\collect_kr_ws.py --minutes %MINUTES% >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
popd

echo [%date% %time%] 종료 코드 %RC% >> "%LOG%"
if not "%RC%"=="0" exit /b %RC%
endlocal

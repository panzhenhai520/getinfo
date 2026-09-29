@echo off
setlocal
set OUT=D:\docker-data\ragflow\ragflow-official-source-8014-probe.log
echo ==== probe started %date% %time% ==== > "%OUT%"
echo USER=%USERNAME% >> "%OUT%" 2>&1
echo CD=%CD% >> "%OUT%" 2>&1
echo. >> "%OUT%"

echo ==== where git ==== >> "%OUT%"
where git >> "%OUT%" 2>&1
git --version >> "%OUT%" 2>&1
echo. >> "%OUT%"

echo ==== where docker ==== >> "%OUT%"
where docker >> "%OUT%" 2>&1
docker version >> "%OUT%" 2>&1
docker compose version >> "%OUT%" 2>&1
echo. >> "%OUT%"

echo ==== where python ==== >> "%OUT%"
where python >> "%OUT%" 2>&1
python --version >> "%OUT%" 2>&1
where py >> "%OUT%" 2>&1
py -0p >> "%OUT%" 2>&1
echo. >> "%OUT%"

echo ==== where node npm ==== >> "%OUT%"
where node >> "%OUT%" 2>&1
node --version >> "%OUT%" 2>&1
where npm >> "%OUT%" 2>&1
npm --version >> "%OUT%" 2>&1
echo. >> "%OUT%"

echo ==== bash / wsl ==== >> "%OUT%"
where bash >> "%OUT%" 2>&1
bash --version >> "%OUT%" 2>&1
where wsl >> "%OUT%" 2>&1
wsl -l -v >> "%OUT%" 2>&1
echo. >> "%OUT%"

echo ==== relevant ports ==== >> "%OUT%"
netstat -ano | findstr /R ":80 :443 :8013 :8014 :9380 :9381 :9382 :9383 :9384 :3306 :3314 :9000 :9014 :1200 :1214 :6379 :6414" >> "%OUT%" 2>&1
echo. >> "%OUT%"

echo ==== docker ps ==== >> "%OUT%"
docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Ports}}\t{{.Status}}" >> "%OUT%" 2>&1
echo ==== probe finished %date% %time% ==== >> "%OUT%"

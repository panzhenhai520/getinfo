@echo off
setlocal
set OUT=D:\docker-data\ragflow\ragflow-official-source-8014-images.log
echo ==== docker images %date% %time% ==== > "%OUT%"
docker images --format "table {{.Repository}}\t{{.Tag}}\t{{.ID}}\t{{.Size}}" >> "%OUT%" 2>&1
echo. >> "%OUT%"
echo ==== docker ps %date% %time% ==== >> "%OUT%"
docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Ports}}\t{{.Status}}" >> "%OUT%" 2>&1

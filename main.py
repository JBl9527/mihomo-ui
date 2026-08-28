from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
import uvicorn
import os
import subprocess

app = FastAPI(title="Mihomo Web Manager V2")
templates = Jinja2Templates(directory="templates")
MIHOMO_CONF = "/etc/mihomo/config.yaml"

def generate_base_yaml(sub_url: str) -> str:
    # 极简订阅模板
    return f"""port: 7890
socks-port: 7891
allow-lan: true
mode: rule
log-level: info
external-controller: 0.0.0.0:9090
secret: "123456"
external-ui: ui

tun:
  enable: true
  stack: gvisor
  auto-route: true
  auto-redirect: true
  auto-detect-interface: true

dns:
  enable: true
  listen: 0.0.0.0:53
  enhanced-mode: fake-ip
  fake-ip-range: 198.20.0.1/16
  nameserver: [223.5.5.5, 119.29.29.29]

proxy-providers:
  机场节点:
    url: "{sub_url}"
    type: http
    interval: 86400
    path: ./providers/airport.yaml
    health-check:
      enable: true
      url: https://www.gstatic.com/generate_204
      interval: 300

proxies:
  - {{name: 直连, type: direct}}

proxy-groups:
  - name: 节点选择
    type: select
    proxies: [自动选择, 直连]
    use: [机场节点]
  - name: 自动选择
    type: url-test
    use: [机场节点]
    tolerance: 50
    interval: 300

rules:
  - GEOSITE,cn,直连
  - GEOIP,CN,直连
  - MATCH,节点选择
"""

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    # 修复了 FastAPI 渲染字典报错的 bug
    return templates.TemplateResponse(request, "index.html")

@app.get("/api/config")
def get_config():
    if os.path.exists(MIHOMO_CONF):
        with open(MIHOMO_CONF, 'r', encoding='utf-8') as f:
            return {"config": f.read()}
    return {"config": "配置文件不存在，请先生成！"}

@app.post("/api/update_sub")
async def update_sub(request: Request):
    data = await request.json()
    sub_url = data.get("sub_url", "").strip()
    if not sub_url:
        return {"status": "warning", "msg": "订阅链接不能为空"}
    try:
        new_yaml = generate_base_yaml(sub_url)
        os.makedirs(os.path.dirname(MIHOMO_CONF), exist_ok=True)
        with open(MIHOMO_CONF, 'w', encoding='utf-8') as f:
            f.write(new_yaml)
        return {"status": "success", "msg": "配置模板已生成！请重启内核生效。"}
    except Exception as e:
        return {"status": "error", "msg": f"生成失败: {str(e)}"}

@app.post("/api/save_raw")
async def save_raw(request: Request):
    data = await request.json()
    raw_yaml = data.get("raw_yaml", "").strip()
    if not raw_yaml:
        return {"status": "warning", "msg": "YAML 内容不能为空"}
    try:
        with open(MIHOMO_CONF, 'w', encoding='utf-8') as f:
            f.write(raw_yaml)
        return {"status": "success", "msg": "自定义 YAML 已保存！请重启内核生效。"}
    except Exception as e:
        return {"status": "error", "msg": f"保存失败: {str(e)}"}

@app.post("/api/restart")
def restart_mihomo():
    try:
        subprocess.run(["systemctl", "restart", "mihomo"], check=True)
        # 顺便检查服务是否处于 active 状态
        status = subprocess.run(["systemctl", "is-active", "mihomo"], capture_output=True, text=True).stdout.strip()
        if status == "active":
            return {"status": "success", "msg": "Mihomo 启动成功，运行正常！"}
        else:
            return {"status": "error", "msg": "内核启动崩溃，请检查 YAML 语法！"}
    except Exception as e:
        return {"status": "error", "msg": f"重启指令失败: {str(e)}"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9621)

from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
import uvicorn
import os
import subprocess

app = FastAPI(title="Mihomo Web Manager")
templates = Jinja2Templates(directory="templates")
MIHOMO_CONF = "/etc/mihomo/config.yaml"

# ==========================================
# 核心基础模板 (融合了你之前的路由和分流规则)
# ==========================================
def generate_base_yaml(sub_url: str) -> str:
    return f"""# ========================
# 自动生成的 Mihomo 配置文件
# ========================
port: 7890
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
  ipv6: false
  enhanced-mode: fake-ip
  fake-ip-range: 198.20.0.1/16
  nameserver:
    - 223.5.5.5
    - 119.29.29.29
  fallback:
    - https://1.1.1.1/dns-query
    - https://8.8.8.8/dns-query

# ========================
# 机场订阅注入
# ========================
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
  - {{name: 拒绝, type: reject}}

proxy-groups:
  - name: 节点选择
    type: select
    proxies:
      - 自动选择
      - 直连
    use: [机场节点]

  - name: 自动选择
    type: url-test
    tolerance: 50
    interval: 300
    use: [机场节点]

rules:
  - GEOSITE,cn,直连
  - GEOIP,CN,直连
  - MATCH,节点选择
"""

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

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
        # 生成新的 yaml 内容
        new_yaml_content = generate_base_yaml(sub_url)
        
        # 写入配置文件
        os.makedirs(os.path.dirname(MIHOMO_CONF), exist_ok=True)
        with open(MIHOMO_CONF, 'w', encoding='utf-8') as f:
            f.write(new_yaml_content)
            
        return {"status": "success", "msg": "配置文件生成成功！请重启内核生效。"}
    except Exception as e:
        return {"status": "error", "msg": f"生成失败: {str(e)}"}

@app.post("/api/restart")
def restart_mihomo():
    try:
        subprocess.run(["systemctl", "restart", "mihomo"], check=True)
        return {"status": "success", "msg": "Mihomo 内核已成功重启！"}
    except Exception as e:
        return {"status": "error", "msg": f"重启失败: {str(e)}"}

if __name__ == "__main__":
    # 使用你指定的 9621 端口
    uvicorn.run(app, host="0.0.0.0", port=9621)

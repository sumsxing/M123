import os
import json
import hashlib
import urllib.request
import urllib.error
import hmac
import hashlib
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
from playwright.sync_api import sync_playwright

# ========== 设置北京时区 ==========
BEIJING_TZ = timezone(timedelta(hours=8))

def now_beijing():
    return datetime.now(BEIJING_TZ)

# ========== 配置区 ==========
URL_TO_MONITOR = "https://alpha123.uk/zh/"
AIRDROP_SECTION = "今日空投"
EMPTY_INDICATORS = ["暂无数据", "无数据", "-", "——", "空", "加载中", "暂无"]
PREVIEW_LENGTH = 800

# ========== COS 配置 ==========
COS_SECRET_ID = os.environ.get("COS_SECRET_ID")
COS_SECRET_KEY = os.environ.get("COS_SECRET_KEY")
COS_BUCKET = os.environ.get("COS_BUCKET")
COS_REGION = os.environ.get("COS_REGION", "ap-guangzhou")
COS_KEY = "monitor_cache.json"

def get_cos_presigned_url(method, expires=3600):
    if not all([COS_SECRET_ID, COS_SECRET_KEY, COS_BUCKET]):
        return None
    host = f"{COS_BUCKET}.cos.{COS_REGION}.myqcloud.com"
    now = datetime.utcnow()
    date_stamp = now.strftime('%Y%m%d')
    amz_date = now.strftime('%Y%m%dT%H%M%SZ')
    credential = f"{COS_SECRET_ID}/{date_stamp}/{COS_REGION}/cos/aws4_request"
    params = {
        'X-Amz-Algorithm': 'AWS4-HMAC-SHA256',
        'X-Amz-Credential': credential,
        'X-Amz-Date': amz_date,
        'X-Amz-Expires': str(expires),
        'X-Amz-SignedHeaders': 'host',
    }
    canonical_uri = f"/{COS_KEY}"
    canonical_querystring = urlencode(sorted(params.items()))
    canonical_headers = f"host:{host}\n"
    signed_headers = "host"
    payload_hash = "UNSIGNED-PAYLOAD"
    canonical_request = f"{method}\n{canonical_uri}\n{canonical_querystring}\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    credential_scope = f"{date_stamp}/{COS_REGION}/cos/aws4_request"
    string_to_sign = f"AWS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n{hashlib.sha256(canonical_request.encode()).hexdigest()}"
    
    def get_signature_key(key, date_stamp, region, service):
        k_date = hmac.new(f"AWS4{key}".encode(), date_stamp.encode(), hashlib.sha256).digest()
        k_region = hmac.new(k_date, region.encode(), hashlib.sha256).digest()
        k_service = hmac.new(k_region, service.encode(), hashlib.sha256).digest()
        k_signing = hmac.new(k_service, "aws4_request".encode(), hashlib.sha256).digest()
        return k_signing
    
    signing_key = get_signature_key(COS_SECRET_KEY, date_stamp, COS_REGION, "cos")
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return f"https://{host}{canonical_uri}?{canonical_querystring}&X-Amz-Signature={signature}"

def cos_get():
    if not all([COS_SECRET_ID, COS_SECRET_KEY, COS_BUCKET]):
        return None
    url = get_cos_presigned_url("GET")
    if not url:
        return None
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        return None
    except Exception as e:
        print(f"COS get error: {e}")
        return None

def cos_put(data):
    if not all([COS_SECRET_ID, COS_SECRET_KEY, COS_BUCKET]):
        return False
    url = get_cos_presigned_url("PUT")
    if not url:
        return False
    body = json.dumps(data, ensure_ascii=False).encode('utf-8')
    try:
        req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'}, method="PUT")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"COS put error: {e}")
        return False

def http_post(url, data, timeout=10):
    try:
        req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        print(f"HTTP post error: {e}")
        return None

def fetch_with_playwright(url, timeout=60):
    """
    使用 Playwright 获取渲染后的页面内容
    """
    try:
        with sync_playwright() as p:
            # 启动浏览器
            browser = p.chromium.launch(headless=True)
            
            context = browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
                viewport={'width': 1920, 'height': 1080}
            )
            
            page = context.new_page()
            
            # 拦截 API 请求
            api_data = None
            
            def handle_response(response):
                nonlocal api_data
                if "api/data" in response.url and response.status == 200:
                    try:
                        api_data = response.json()
                        print(f"✓ Intercepted API data from: {response.url}")
                    except Exception as e:
                        print(f"✗ Failed to parse API response: {e}")
            
            page.on("response", handle_response)
            
            # 访问页面
            print(f"Navigating to {url}...")
            page.goto(url, wait_until='networkidle', timeout=timeout*1000)
            
            # 等待 JS 执行
            page.wait_for_timeout(5000)
            
            # 如果拦截到 API 数据，直接返回
            if api_data:
                print(f"✓ Using intercepted API data")
                browser.close()
                return api_data
            
            # 否则从页面提取表格数据
            print("Extracting from DOM...")
            table_data = page.evaluate('''() => {
                const rows = document.querySelectorAll('table tr');
                const data = [];
                for (let i = 1; i < rows.length; i++) {
                    const cells = rows[i].querySelectorAll('td');
                    if (cells.length >= 2) {
                        const name = cells[0]?.innerText?.trim() || '';
                        const points = cells[1]?.innerText?.trim() || '';
                        const amount = cells[2]?.innerText?.trim() || '';
                        const time = cells[3]?.innerText?.trim() || '';
                        if (name && name !== '项目' && name !== '暂无数据') {
                            data.push({name, points, amount, time});
                        }
                    }
                }
                return data;
            }''')
            
            browser.close()
            return table_data
            
    except Exception as e:
        print(f"✗ Playwright failed: {e}")
        import traceback
        print(traceback.format_exc())
        return None

def parse_projects(data):
    """
    解析项目数据
    """
    if not data:
        return [], False
    
    projects = []
    
    if isinstance(data, list):
        projects = data
    elif isinstance(data, dict):
        for key in ['today', 'airdrops', 'data', 'items', 'list', 'result']:
            if key in data and isinstance(data[key], list):
                projects = data[key]
                break
        
        if not projects:
            for key, value in data.items():
                if isinstance(value, list) and len(value) > 0:
                    projects = value
                    break
    
    # 过滤空数据
    has_data = len(projects) > 0
    
    if has_data and len(projects) == 1:
        project = projects[0]
        if isinstance(project, dict):
            values_str = ' '.join(str(v).lower() for v in project.values())
            for indicator in EMPTY_INDICATORS:
                if indicator.lower() in values_str:
                    has_data = False
                    break
    
    return projects, has_data

def format_projects(projects, max_length=800):
    """格式化项目列表"""
    if not projects:
        return "暂无数据"
    
    lines = []
    for i, project in enumerate(projects[:5], 1):
        if isinstance(project, dict):
            name = (project.get('name') or 
                   project.get('project') or 
                   project.get('symbol') or 
                   project.get('title') or 
                   'Unknown')
            
            points = (project.get('points') or 
                     project.get('积分') or 
                     project.get('score') or 
                     '-')
            
            amount = (project.get('amount') or 
                     project.get('数量') or 
                     project.get('quantity') or 
                     '-')
            
            time = (project.get('time') or 
                   project.get('时间') or 
                   project.get('datetime') or 
                   project.get('date') or 
                   '-')
            
            lines.append(f"{i}. {name} | 积分: {points} | 数量: {amount} | 时间: {time}")
        else:
            lines.append(f"{i}. {str(project)}")
    
    if len(projects) > 5:
        lines.append(f"... 还有 {len(projects) - 5} 个项目")
    
    result = '\n'.join(lines)
    if len(result) > max_length:
        result = result[:max_length] + "\n..."
    return result

def send_notification(key, title, message, detail=""):
    """发送企业微信通知"""
    if not key:
        print("Warning: WECHAT_KEY not set")
        return False
    
    webhook_url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}"
    full_message = f"**{title}**\n\n{message}\n"
    if detail:
        if len(detail) > 2000:
            detail = detail[:2000] + "\n..."
        full_message += f"\n**详情：**\n```\n{detail}\n```"
    full_message += f"\n\n*时间：{now_beijing().strftime('%Y-%m-%d %H:%M:%S')}*"
    
    data = {"msgtype": "markdown", "markdown": {"content": full_message}}
    result = http_post(webhook_url, data)
    if result and result.get("errcode") == 0:
        print("✓ Notification sent successfully")
        return True
    else:
        print(f"✗ Failed to send notification: {result}")
        return False

def main():
    """主函数"""
    print(f"=== Alpha123 Monitor Started at {now_beijing()} ===")
    
    wechat_key = os.environ.get("WECHAT_KEY")
    if not wechat_key:
        print("✗ Error: WECHAT_KEY not set")
        return 1
    
    # 从 COS 读取上次状态
    print("Reading cache from COS...")
    last_data = cos_get() or {}
    last_has_data = last_data.get('has_data', False)
    last_projects_hash = last_data.get('projects_hash', '')
    
    print(f"Last state: has_data={last_has_data}, hash={last_projects_hash[:8] if last_projects_hash else 'None'}")
    
    # 使用 Playwright 获取数据
    print("Fetching data with Playwright...")
    raw_data = fetch_with_playwright(URL_TO_MONITOR, timeout=60)
    
    if raw_data is None:
        print("✗ Failed to fetch data")
        send_notification(wechat_key, "❌ 监控失败", 
            "无法获取 alpha123.uk 数据，Playwright 执行失败")
        return 1
    
    # 解析数据
    projects, has_data = parse_projects(raw_data)
    projects_hash = hashlib.md5(json.dumps(projects, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    projects_preview = format_projects(projects, PREVIEW_LENGTH)
    
    print(f"Current: has_data={has_data}, projects={len(projects)}, hash={projects_hash[:8]}")
    print(f"Preview: {projects_preview[:200]}...")
    
    # 检测逻辑
    alert_triggered = False
    alert_title = ""
    alert_message = ""
    alert_detail = ""
    
    is_first_run = not last_projects_hash
    
    if is_first_run:
        print("First run detected")
        alert_triggered = True
        if has_data:
            alert_title = "✅ 监控已启动 - 今日空投有数据"
            alert_message = f"**目标：** alpha123.uk\n**状态：** 检测到 {len(projects)} 个今日空投项目"
        else:
            alert_title = "✅ 监控已启动 - 今日空投为空"
            alert_message = "**目标：** alpha123.uk\n**状态：** 当前暂无今日空投项目，等待更新..."
        alert_detail = projects_preview
    
    else:
        data_changed = projects_hash != last_projects_hash
        
        # 核心：从空到有数据
        if not last_has_data and has_data:
            alert_triggered = True
            alert_title = "🚨 今日空投更新！"
            alert_message = f"**发现 {len(projects)} 个新的空投项目！**\n\n💡 有新的空投发布了，请尽快查看！"
            alert_detail = projects_preview
        
        # 数据内容变化
        elif last_has_data and has_data and data_changed:
            alert_triggered = True
            alert_title = "📢 今日空投数据变化"
            alert_message = f"**空投项目数据已更新**\n当前项目数：{len(projects)}"
            alert_detail = projects_preview
        
        # 数据清空
        elif last_has_data and not has_data:
            alert_triggered = True
            alert_title = "⚠️ 今日空投已结束"
            alert_message = "**所有今日空投项目已下架或结束**"
            alert_detail = "当前暂无进行中的空投项目"
    
    # 保存状态
    print("Saving cache to COS...")
    cos_put(cache_data := {
        'has_data': has_data,
        'projects_hash': projects_hash,
        'projects_count': len(projects),
        'projects_preview': projects_preview[:1000],
        'timestamp': now_beijing().isoformat()
    })
    
    # 发送通知
    if alert_triggered:
        print(f"Sending alert: {alert_title}")
        send_notification(wechat_key, alert_title, alert_message, alert_detail)
    else:
        status = f"{len(projects)}个项目" if has_data else "为空"
        print(f"No alert needed. Status: {status}")
    
    print(f"=== Monitor Finished ===")
    return 0

if __name__ == "__main__":
    exit(main())

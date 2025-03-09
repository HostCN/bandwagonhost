import os
import json
import logging
import asyncio
import aiohttp
import re
from bs4 import BeautifulSoup, Comment
import telegram
from telegram.error import TimedOut
from dotenv import load_dotenv
import certifi
import ssl
import random

# 加载环境变量
load_dotenv()

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# 从环境变量中获取配置
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
MAX_RETRIES = int(os.getenv('MAX_RETRIES', 1))      # 最大重试次数
TIMEOUT = int(os.getenv('TIMEOUT', 30))             # 请求超时时间（秒）
BASE_URL = "https://bwh81.net"                      # 站点的基础 URL
CONFIG_FILE = "/www/wwwroot/bwh/config.json"        # 配置文件路径
MAX_CONCURRENT_REQUESTS = 2                         # 最大并发请求数

# 配置翻译字典
config_translation = {
    'SSD': '硬盘',
    'RAM': '内存',
    'CPU': 'CPU',
    'Transfer': '流量',
    'Link speed': '带宽',
    'Location': '地区',
    'Backups': '备份',
    'Snapshots': '快照',
    'OS': 'OS',
    'IPv4': 'IPv4'
}

###############################################################################
# 动态加载 config.json 的工具函数
###############################################################################
def load_config(file_path):
    """加载 config.json 文件并返回 monitor_urls"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        monitor_urls = config.get('monitor_urls')
        if not monitor_urls or not isinstance(monitor_urls, list):
            raise ValueError(f"{file_path} 中的 'monitor_urls' 必须是一个非空列表")
        return monitor_urls
    except FileNotFoundError:
        logging.error(f"未找到 {file_path} 文件，请确保配置文件存在")
        raise
    except json.JSONDecodeError:
        logging.error(f"{file_path} 文件格式错误，请检查 JSON 语法")
        raise
    except ValueError as e:
        logging.error(str(e))
        raise

def get_config_mtime(file_path):
    """获取 config.json 文件的最后修改时间"""
    try:
        return os.path.getmtime(file_path)
    except FileNotFoundError:
        return None

###############################################################################
# 商品状态跟踪类
###############################################################################
class ProductTracker:
    def __init__(self, file_path="/www/wwwroot/bwh/products.json"):
        self.file_path = file_path
        self.inventory = {}
        self.load_from_file()
        
    def load_from_file(self):
        try:
            os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
            if os.path.exists(self.file_path):
                with open(self.file_path, "r", encoding="utf-8") as f:
                    self.inventory = json.load(f)
                logging.info("产品数据加载成功")
            else:
                logging.info(f"没有找到文件 {self.file_path}，将创建新文件")
                self.inventory = {}
        except Exception as e:
            logging.error(f"加载产品数据失败: {e}")
            self.inventory = {}
            
    def save_to_file(self):
        try:
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(self.inventory, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logging.error("保存产品数据到文件失败: " + str(e))
            
    def update_product(self, key, price, features, link, out_of_stock, message_id=None):
        data = {
            "price": price,
            "features": features,
            "link": link,
            "out_of_stock": out_of_stock,
        }
        if message_id is not None:
            data["message_id"] = message_id
        else:
            if key in self.inventory and "message_id" in self.inventory[key]:
                data["message_id"] = self.inventory[key]["message_id"]
        self.inventory[key] = data
        self.save_to_file()
        
    def get_product(self, key):
        return self.inventory.get(key)

# 初始化全局状态
product_tracker = ProductTracker()

###############################################################################
# 辅助函数
###############################################################################
def make_product_key(source, name):
    return f"{source}::{name}"

def extract_promo_code(soup):
    comments = soup.find_all(string=lambda text: isinstance(text, Comment))
    for comment in comments:
        if "promo code:" in comment.lower():
            parts = comment.split("promo code:")
            if len(parts) > 1:
                promo_code = parts[1].strip().rstrip("-->").strip()
                return promo_code
    return None

async def send_telegram_message(message):
    bot = telegram.Bot(token=TELEGRAM_TOKEN)
    retries = 0
    while retries < MAX_RETRIES:
        try:
            sent_message = await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=message,
                parse_mode='HTML'
            )
            logging.info(f"消息发送成功: {message}")
            return sent_message
        except TimedOut:
            retries += 1
            logging.warning(f"发送超时，正在重试... {retries}/{MAX_RETRIES}")
            await asyncio.sleep(2)
        except Exception as e:
            logging.error(f"发送消息失败: {e}")
            break

async def edit_or_skip_message(existing_product_key, new_message):
    bot = telegram.Bot(token=TELEGRAM_TOKEN)
    existing_product = product_tracker.get_product(existing_product_key)
    if not existing_product or "message_id" not in existing_product:
        logging.info("没有找到有效的 message_id，跳过编辑操作")
        return
    message_id = existing_product["message_id"]
    try:
        await bot.edit_message_text(
            chat_id=TELEGRAM_CHAT_ID,
            message_id=message_id,
            text=new_message,
            parse_mode='HTML'
        )
        logging.info("编辑消息成功")
    except telegram.error.BadRequest as e:
        if "Message is not modified" in str(e):
            logging.info("消息内容未更改，跳过编辑操作")
        elif "Message_id_invalid" in str(e):
            logging.error("消息已被删除或不存在")
            product_tracker.update_product(
                existing_product_key,
                existing_product['price'],
                existing_product['features'],
                existing_product['link'],
                existing_product['out_of_stock'],
                message_id=None
            )
        else:
            logging.error(f"编辑消息失败: {e}")
    except Exception as e:
        logging.error(f"编辑消息失败: {e}")

def build_product_message(name, price, features_str, full_link, out_of_stock=False, prefix=None, promo_code=None):
    period_translation = {
        "Monthly": "每月",
        "Quarterly": "每季",
        "Semi-Annually": "半年",
        "Annually": "每年"
    }
    price_lines = []
    for m in re.finditer(r'(\$[0-9.,]+\s*USD)\s*(Monthly|Quarterly|Semi-Annually|Annually)', price):
        price_val, period = m.group(1), m.group(2)
        price_lines.append(f"{price_val} {period_translation.get(period, period)}")
    if not price_lines:
        price_lines = [p.strip() for p in price.split("<br />") if p.strip()]

    translated_features = []
    for feature in features_str.split('<br />'):
        feature = feature.strip()
        for key, value in config_translation.items():
            if key in feature:
                feature = feature.replace(key, value)
        translated_features.append(feature)

    header = f"✅ {prefix} - {name}" if prefix and not out_of_stock else f"❌ 搬瓦工 - {name} - 已下架或已售罄" if out_of_stock else name
    message_lines = [header, "", "🔧 配置:"]
    for feat in translated_features:
        message_lines.append(f"• {feat}")
    message_lines.extend(["", "💰 价格:"])
    for pl in price_lines:
        message_lines.append(f"• {pl}")
    if promo_code:
        message_lines.extend(["", f"🏷️ 优惠码: <code>{promo_code}</code>", ""])
    if full_link:
        link_text = f"🛍️ 购买链接: {full_link}" if not out_of_stock else f"🛍️ 购买链接: <s>{full_link}</s>"
        message_lines.append(link_text)
    return "\n".join(message_lines)

###############################################################################
# 获取并解析页面内容
###############################################################################
async def fetch_and_parse_products(url, send_notifications=False, semaphore=None):
    source = url
    current_product_keys = set()
    retries = 0
    full_link = re.sub(r'cart\.php\?a=add', 'aff.php?aff=55580', url)
    if 'aff=' not in full_link:
        full_link += ('&' if '?' in full_link else '?') + 'aff=55580'

    ssl_context = ssl.create_default_context(cafile=certifi.where())
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context)) as session:
        while retries < MAX_RETRIES:
            async with semaphore:  # 使用信号量限制并发
                try:
                    async with session.get(url, timeout=TIMEOUT, allow_redirects=True) as response:
                        if response.status in [400, 401, 403, 404]:
                            logging.error(f"请求 {url} 返回状态码 {response.status}，跳过解析")
                            return
                        elif response.status == 503:
                            logging.warning(f"请求 {url} 返回 503 状态码，可能因并发过多，稍后重试")
                            raise Exception("503 Service Unavailable")
                        elif response.status != 200:
                            logging.warning(f"请求 {url} 返回非 200 状态码: {response.status}，仍尝试解析内容")

                        final_url = str(response.url)
                        if final_url == 'https://bwh81.net/cart.php':
                            logging.info(f"监控链接 {url} 跳转到 {final_url}，跳过处理")
                            return

                        html = await response.text()
                        soup = BeautifulSoup(html, 'html.parser')
                        
                        title_tag = soup.find("title")
                        if not title_tag or not title_tag.get_text(strip=True):
                            logging.error(f"网站 {url} 的<title>标签为空，跳过解析")
                            return
                        title_text = title_tag.get_text(strip=True)
                        
                        if "维护" in title_text or "Maintenance" in title_text:
                            logging.info(f"网站 {url} 显示维护页面，跳过解析")
                            return
                        if "Bandwagon" not in title_text:
                            logging.info(f"网站 {url} 的<title>标签中不包含预期字段 'Bandwagon'，跳过解析")
                            return
                        break
                except Exception as e:
                    retries += 1
                    logging.warning(f"请求 {url} 失败（状态码或网络错误: {e}），正在重试... {retries}/{MAX_RETRIES}")
                    await asyncio.sleep(2 + retries * 2)  # 动态增加重试间隔
                    if retries == MAX_RETRIES:
                        logging.error(f"请求 {url} 达到最大重试次数，放弃: {e}")
                        return

        promo_code = extract_promo_code(soup)
        if promo_code:
            logging.info(f"获取到优惠码: {promo_code}")

        out_of_stock = soup.find('div', class_='errorbox', string="Out of Stock") is not None
        logging.info(f"监控链接 {url} 库存情况: {'无货' if out_of_stock else '有货'}")

        if out_of_stock:
            name = "未知商品"
            pid_match = re.search(r'pid=(\d+)', url)
            if pid_match:
                name = f"商品 PID {pid_match.group(1)}"
            price = "价格未知"
            features = "配置未知"
        else:
            product = soup.find('div', class_='cartbox')
            if not product:
                logging.warning(f"页面 {url} 未找到商品信息")
                return

            name_tag = product.find('strong')
            raw_name = name_tag.get_text(strip=True) if name_tag else "未知商品"
            name = raw_name.replace("Basic VPS - Self-managed - ", "").strip()
            
            price_tag = soup.find('select', {'name': 'billingcycle'})
            price = price_tag.get_text(separator="<br />", strip=True) if price_tag else "价格未提供"

            features = product.get_text(separator="<br />", strip=True).replace(raw_name, "").strip()
            allowed_features = ["SSD", "RAM", "CPU", "Transfer", "Link speed", "Location"]
            features_lines = features.split('<br />')
            filtered_features = [
                line.strip() for line in features_lines
                if any(line.strip().startswith(key) for key in allowed_features)
            ]
            features = "<br />".join(filtered_features) if filtered_features else "配置未知"

        key = make_product_key(source, name)
        current_product_keys.add(key)
        existing_product = product_tracker.get_product(key)

        if not out_of_stock:
            if existing_product and existing_product.get('out_of_stock'):
                logging.info(f"检测到 {name} 已重新上架，发送补货通知")
                message = build_product_message(name, price, features, full_link, out_of_stock=False, prefix="搬瓦工补货", promo_code=promo_code)
            elif not existing_product or existing_product['price'] != price or existing_product['features'] != features:
                logging.info(f"检测到 {name} 有货或信息更新，发送新通知")
                message = build_product_message(name, price, features, full_link, out_of_stock=False, prefix="搬瓦工上新", promo_code=promo_code)
            else:
                return

            if send_notifications:
                sent_message = await send_telegram_message(message)
                product_tracker.update_product(key, price, features, full_link, out_of_stock, message_id=sent_message.message_id if sent_message else None)
            else:
                product_tracker.update_product(key, price, features, full_link, out_of_stock)

        else:
            if existing_product and not existing_product.get('out_of_stock'):
                logging.info(f"检测到 {name} 已无货，编辑旧消息")
                message = build_product_message(name, existing_product['price'], existing_product['features'], full_link, out_of_stock=True, promo_code=promo_code)
                if send_notifications and "message_id" in existing_product and existing_product["message_id"]:
                    await edit_or_skip_message(key, message)
                product_tracker.update_product(key, existing_product['price'], existing_product['features'], full_link, out_of_stock, message_id=existing_product.get("message_id"))
            elif not existing_product:
                product_tracker.update_product(key, price, features, full_link, out_of_stock)

        for key in list(product_tracker.inventory.keys()):
            if key.startswith(f"{source}::") and key not in current_product_keys:
                existing_product = product_tracker.get_product(key)
                if not existing_product.get('out_of_stock'):
                    display_name = key.split("::", 1)[1]
                    if send_notifications and "message_id" in existing_product and existing_product["message_id"]:
                        await edit_or_skip_message(
                            key,
                            build_product_message(display_name, existing_product['price'], existing_product['features'], full_link, out_of_stock=True, promo_code=promo_code)
                        )
                    product_tracker.update_product(
                        key,
                        existing_product['price'],
                        existing_product['features'],
                        full_link,
                        True,
                        message_id=existing_product.get("message_id")
                    )

###############################################################################
# 定时任务
###############################################################################
async def periodic_task():
    last_mtime = get_config_mtime(CONFIG_FILE)
    monitor_urls = load_config(CONFIG_FILE)
    first_run = True
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)  # 限制并发请求数

    while True:
        current_mtime = get_config_mtime(CONFIG_FILE)
        if current_mtime != last_mtime:
            logging.info(f"检测到 {CONFIG_FILE} 文件已更新，重新加载配置")
            try:
                monitor_urls = load_config(CONFIG_FILE)
                last_mtime = current_mtime
                logging.info(f"新的监控链接: {monitor_urls}")
            except Exception as e:
                logging.error(f"重新加载配置失败: {e}，继续使用旧配置")

        tasks = []
        for url in monitor_urls:
            if first_run:
                logging.info(f"首次运行：仅保存 {url} 商品信息，不发送通知")
                tasks.append(fetch_and_parse_products(url, send_notifications=False, semaphore=semaphore))
            else:
                logging.info(f"监控 {url} 商品状态变化")
                tasks.append(fetch_and_parse_products(url, send_notifications=True, semaphore=semaphore))
            await asyncio.sleep(random.uniform(0.5, 2))  # 在每个任务间添加随机延迟

        await asyncio.gather(*tasks)
        first_run = False
        logging.info(f"完成一轮监控，当前并发限制: {MAX_CONCURRENT_REQUESTS}")
        await asyncio.sleep(30)  # 每轮监控间隔 30 秒

###############################################################################
# 主函数
###############################################################################
async def main():
    logging.info("启动库存监控任务...")
    await periodic_task()

if __name__ == "__main__":
    asyncio.run(main())

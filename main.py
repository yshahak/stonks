import datetime
import glob
import json
import logging
import os
import time
import brotli
import requests
import yfinance as yf

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
SYMBOLS_DIR = os.path.join(SCRIPT_DIR, 'symbols')
DIST_DIR = os.path.join(SCRIPT_DIR, 'dist')

# debug: uncomment to process only these symbol files (names without .json)
# ONLY = ['TEVA']
ONLY = []


def get_latest_user_agent(operating_system='windows', browser='chrome'):
    url = f'https://jnrbsn.github.io/user-agents/user-agents.json'
    r = requests.get(url)
    r.raise_for_status()
    user_agents = r.json()

    for user_agent in user_agents:
        if operating_system.lower() in user_agent.lower() and browser.lower() in user_agent.lower():
            return user_agent

    return None


def fetch_justetf(symbol, currency):
    url = f'https://www.justetf.com/api/etfs/{symbol}/quote?locale=en&currency={currency}&isin={symbol}'
    user_agent = get_latest_user_agent(operating_system='windows', browser='chrome')
    r = requests.get(url, headers={'User-Agent': user_agent, 'Accept': 'application/json'})
    r.raise_for_status()
    info = r.json()
    return info['latestQuote']['raw'], info['latestQuoteDate']


def fetch_yahoo_finance(symbol):
    close = yf.Ticker(symbol).history()['Close'].dropna()
    return close.iloc[-1], close.index[-1].strftime('%Y-%m-%d')


def parse_tase_security(response):
    price = response['LastRate'] / 100  # ILA -> ILS
    return price, datetime.datetime.strptime(response['TradeDate'], "%d/%m/%Y").strftime('%Y-%m-%d')


def parse_tase_mutual_fund(response):
    price = response['purchasePrice'] / 100  # ILA -> ILS
    return price, datetime.datetime.strptime(response['ratesAsOf'], "%Y-%m-%d").strftime('%Y-%m-%d')


def get_tase_api_json(url, site):
    r = requests.get(
        url,
        headers={'User-Agent': get_latest_user_agent(operating_system='windows', browser='chrome'),
                 'Origin': f'https://{site}.tase.co.il',
                 'Referer': f'https://{site}.tase.co.il/'},
        timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_issa_direct(symbol, symbol_type):
    if symbol_type == 'etf':
        response = get_tase_api_json(f'https://api.tase.co.il/api/company/securitydata?securityId={symbol}&lang=0', 'market')
        return parse_tase_security(response)

    response = get_tase_api_json(f'https://maya.tase.co.il/api/v1/funds/mutual/{symbol}', 'maya')
    return parse_tase_mutual_fund(response)


def get_issa_rest_api_response(request):
    if 400 <= request.response.status_code < 600:
        raise Exception(f'Status code {request.response.status_code}')

    response = brotli.decompress(request.response.body)
    response = response.decode('utf-8')
    response = json.loads(response)
    return response


def fetch_issa_selenium_attempt(symbol, symbol_type, wait):
    """Loads the TASE page in a browser and intercepts its API call. Returns (price, date) or (0, '')."""
    # imported lazily: the browser is only needed when the direct API fails
    import chromedriver_autoinstaller
    from selenium.webdriver.chrome.options import Options
    from seleniumwire import webdriver

    logging.getLogger("seleniumwire").setLevel(logging.ERROR)
    chromedriver_autoinstaller.install()

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1920,980")
    driver = webdriver.Chrome(options=options)
    try:
        if symbol_type == 'etf':
            driver.get(f"https://market.tase.co.il/he/market_data/security/{symbol}")
        else:
            driver.get(f"https://maya.tase.co.il/he/funds/mutual-funds/{symbol}")

        time.sleep(wait)
        driver.implicitly_wait(wait)

        price, price_date = 0, ''
        for request in driver.requests:
            if not request.response:
                continue
            if request.url.startswith('https://api.tase.co.il/api/company/securitydata'):
                price, price_date = parse_tase_security(get_issa_rest_api_response(request))
            if request.url.startswith('https://maya.tase.co.il/api/v1/funds/mutual'):
                price, price_date = parse_tase_mutual_fund(get_issa_rest_api_response(request))
        return price, price_date
    finally:
        driver.quit()


def fetch_issa_selenium(symbol, symbol_type):
    for attempt in range(1, 10):
        price, price_date = fetch_issa_selenium_attempt(symbol, symbol_type, wait=30 * attempt)
        if price:
            return price, price_date
    return 0, ''


def fetch_issa(symbol, symbol_type):
    try:
        return fetch_issa_direct(symbol, symbol_type)
    except Exception as e:
        logging.warning(f"direct TASE API failed for {symbol} ({e}), falling back to selenium")
        return fetch_issa_selenium(symbol, symbol_type)


def fetch_price(symbol_track_info):
    """Returns (price, date 'YYYY-MM-DD') for the symbol, using its configured source."""
    source = symbol_track_info['source']
    symbol = symbol_track_info['symbol']

    if source == 'justetf':
        return fetch_justetf(symbol, symbol_track_info['currency'])
    if source == 'yahoo_finance':
        return fetch_yahoo_finance(symbol)
    if source == 'issa':
        return fetch_issa(symbol, symbol_track_info['type'])
    return 0, ''


def write_symbol_output(symbol_track_info):
    symbol_dist_dir = os.path.join(DIST_DIR, symbol_track_info['id'])
    os.makedirs(symbol_dist_dir, exist_ok=True)

    files = {
        'price': str(symbol_track_info['price']),
        'currency': symbol_track_info['currency'],
        'date': symbol_track_info['price_date'],
        'info.json': json.dumps(symbol_track_info),
    }
    for name, content in files.items():
        with open(os.path.join(symbol_dist_dir, name), 'w+') as f:
            f.write(content)


def process_symbol_file(symbol_track_file_path):
    with open(symbol_track_file_path) as f:
        symbol_track_info = json.load(f)

    price, price_date = fetch_price(symbol_track_info)
    if not price:
        raise Exception(f"Failed to get price for {symbol_track_info['symbol']}")

    symbol_track_info['price'] = price
    symbol_track_info['price_date'] = price_date
    write_symbol_output(symbol_track_info)

    logging.info(f'symbol "{symbol_track_info["id"]}" update completed. price: {price} {symbol_track_info["currency"]} date: {price_date}')


def main():
    logging.info(f"reading symbols *.json files in {SYMBOLS_DIR} ...")
    for symbol_track_file_path in sorted(glob.glob(os.path.join(SYMBOLS_DIR, '*.json'), recursive=True)):
        if ONLY and os.path.splitext(os.path.basename(symbol_track_file_path))[0] not in ONLY:
            continue
        logging.info(f"processing {symbol_track_file_path} ...")
        try:
            process_symbol_file(symbol_track_file_path)
        except Exception:
            logging.exception(f'Failed to process {symbol_track_file_path}')
            raise


if __name__ == '__main__':
    main()

from flask import Flask, render_template, request, jsonify, send_from_directory
from flask_socketio import SocketIO, emit
import requests
import time
import threading
import os
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv("SECRET_KEY")
socketio = SocketIO(app, async_mode='eventlet')

# Serve static files from the 'static' directory
@app.route('/static/<path:path>')
def serve_static(path):
    return send_from_directory('static', path)

# Replace with your Helius API key
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")

tracked_accounts = {}
latest_signatures = {}
# Track the number of transaction history requests per wallet address
history_request_counts = {}  # {wallet_address: count}
MAX_HISTORY_REQUESTS = 5  # Maximum number of history requests allowed

def fetch_transactions(wallet_address, limit=5, before=None):
    url = f"https://api.helius.xyz/v0/addresses/{wallet_address}/transactions?api-key={HELIUS_API_KEY}&limit={limit}"
    if before:
        url += f"&before={before}"
    headers = {
        "accept": "application/json"
    }
    try:
        response = requests.get(url, headers=headers)
        print(f"URL: {url}")
        print(f"Status code: {response.status_code}")
        print(f"Response: {response.text}")
        if response.status_code == 200:
            data = response.json()
            return [{"signature": tx["signature"], "full_data": tx} for tx in data]
        else:
            return {"error": f"Lỗi từ Helius API - Status {response.status_code}: {response.text}"}
    except Exception as e:
        return {"error": f"Lỗi kết nối: {str(e)}"}

def fetch_transaction_details(signature):
    url = f"https://api.helius.xyz/v0/transactions?api-key={HELIUS_API_KEY}"
    payload = {
        "transactions": [signature]
    }
    headers = {
        "accept": "application/json",
        "Content-Type": "application/json"
    }
    try:
        response = requests.post(url, json=payload, headers=headers)
        if response.status_code == 200:
            data = response.json()
            return data[0] if data else {"error": "Không tìm thấy chi tiết giao dịch"}
        else:
            return {"error": f"Lỗi từ Helius API - Status {response.status_code}: {response.text}"}
    except Exception as e:
        return {"error": f"Lỗi kết nối: {str(e)}"}

def fetch_token_metadata(token_address):
    url = f"https://api.helius.xyz/v0/tokens/metadata?api-key={HELIUS_API_KEY}"
    payload = {
        "mintAccounts": [token_address]
    }
    headers = {
        "accept": "application/json",
        "Content-Type": "application/json"
    }
    try:
        response = requests.post(url, json=payload, headers=headers)
        if response.status_code == 200:
            data = response.json()
            if data and len(data) > 0:
                return data[0]["offChainData"]["name"], data[0]["offChainData"]["symbol"]
        return "Unknown Token", "UNKNOWN"
    except Exception as e:
        print(f"Error fetching token metadata: {str(e)}")
        return "Unknown Token", "UNKNOWN"

def fetch_token_price(token_symbol):
    token_id_map = {
        "SOL": "solana",
        "USDC": "usd-coin",
        "USDT": "tether",
    }
    coingecko_id = token_id_map.get(token_symbol, token_symbol.lower())
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={coingecko_id}&vs_currencies=usd"
    try:
        response = requests.get(url)
        if response.status_code == 200:
            data = response.json()
            return data.get(coingecko_id, {}).get("usd", 0)
        return 0
    except Exception as e:
        print(f"Error fetching token price: {str(e)}")
        return 0

def analyze_transaction_details(tx_details):
    actions = []
    token_info = []
    total_value_usd = 0

    if not tx_details:
        return ["Unknown"], token_info, total_value_usd

    if "type" in tx_details:
        if tx_details["type"] == "TRANSFER":
            actions.append("SOL Transfer")
            amount = tx_details.get("nativeTransfers", [{}])[0].get("amount", 0) / 1_000_000_000
            token_name, token_symbol = "Solana", "SOL"
            price = fetch_token_price(token_symbol)
            value_usd = amount * price
            total_value_usd += value_usd
            token_info.append({
                "token_name": token_name,
                "token_symbol": token_symbol,
                "amount": amount,
                "value_usd": value_usd
            })
        elif tx_details["type"] == "SWAP":
            actions.append("Swap")
        elif tx_details["type"] == "NFT_MINT":
            actions.append("NFT Mint")

    if "events" in tx_details:
        if "fungible" in tx_details["events"]:
            for transfer in tx_details["events"]["fungible"]:
                actions.append("Token Transfer")
                token_address = transfer.get("tokenAddress")
                amount = transfer.get("amount", 0) / (10 ** transfer.get("decimals", 0))
                token_name, token_symbol = fetch_token_metadata(token_address)
                price = fetch_token_price(token_symbol)
                value_usd = amount * price
                total_value_usd += value_usd
                token_info.append({
                    "token_name": token_name,
                    "token_symbol": token_symbol,
                    "amount": amount,
                    "value_usd": value_usd
                })
        if "nft" in tx_details["events"]:
            actions.append("NFT Transfer")

    return actions if actions else ["Unknown"], token_info, total_value_usd

def monitor_wallet(wallet_address):
    global latest_signatures
    while wallet_address in tracked_accounts:
        transactions = fetch_transactions(wallet_address, limit=1)
        if isinstance(transactions, dict) and "error" in transactions:
            socketio.emit('notification', {'message': transactions["error"]}, broadcast=True)
            time.sleep(60)
            continue

        if wallet_address not in latest_signatures:
            latest_signatures[wallet_address] = None

        latest_tx = transactions[0]["signature"] if transactions else None
        if latest_tx and latest_tx != latest_signatures.get(wallet_address):
            latest_signatures[wallet_address] = latest_tx
            tx_details = transactions[0]["full_data"]
            actions, token_info, total_value_usd = analyze_transaction_details(tx_details)
            socketio.emit('notification', {
                'message': f"New transaction detected for {wallet_address}: {latest_tx}",
                'actions': actions,
                'token_info': token_info,
                'total_value_usd': total_value_usd,
                'link': f"https://solscan.io/tx/{latest_tx}"
            }, broadcast=True)

        time.sleep(60)

@app.route('/') 
def home():
    return render_template('index.html')

@app.route('/track', methods=['POST'])
def track():
    data = request.get_json()
    wallet_address = data.get('wallet_address') if data else None
    if not wallet_address:
        return jsonify({"error": "Vui lòng nhập địa chỉ ví!"}), 400

    print(f"Tracking wallet: {wallet_address}")
    if wallet_address not in tracked_accounts:
        thread = threading.Thread(target=monitor_wallet, args=(wallet_address,), daemon=True)
        thread.start()
        tracked_accounts[wallet_address] = thread
        print(f"Added {wallet_address} to tracked_accounts: {tracked_accounts.keys()}")

    result = fetch_transactions(wallet_address, limit=5)
    if isinstance(result, dict) and "error" in result:
        return jsonify(result), 400

    enriched_transactions = []
    for tx in result:
        tx_details = tx["full_data"]
        actions, token_info, total_value_usd = analyze_transaction_details(tx_details)
        enriched_transactions.append({
            "signature": tx["signature"],
            "actions": actions,
            "token_info": token_info,
            "total_value_usd": total_value_usd,
            "link": f"https://solscan.io/tx/{tx['signature']}"
        })

    return jsonify({"transactions": enriched_transactions})

@app.route('/tracked_accounts', methods=['GET'])
def get_tracked_accounts():
    print(f"Returning tracked accounts: {list(tracked_accounts.keys())}")
    return jsonify({"accounts": list(tracked_accounts.keys())})

@app.route('/delete_account', methods=['POST'])
def delete_account():
    data = request.get_json()
    wallet_address = data.get('wallet_address') if data else None
    if not wallet_address:
        return jsonify({"error": "Vui lòng cung cấp địa chỉ ví!"}), 400

    if wallet_address in tracked_accounts:
        del tracked_accounts[wallet_address]
        if wallet_address in latest_signatures:
            del latest_signatures[wallet_address]
        return jsonify({"message": f"Đã xóa {wallet_address} khỏi danh sách theo dõi"})
    else:
        return jsonify({"error": "Địa chỉ ví không có trong danh sách theo dõi"}), 404

@app.route('/transaction_history', methods=['POST'])
def transaction_history():
    data = request.get_json()
    wallet_address = data.get('wallet_address') if data else None
    if not wallet_address:
        return jsonify({"error": "Vui lòng cung cấp địa chỉ ví!"}), 400

    # Track the number of history requests
    if wallet_address not in history_request_counts:
        history_request_counts[wallet_address] = 0
    history_request_counts[wallet_address] += 1

    if history_request_counts[wallet_address] > MAX_HISTORY_REQUESTS:
        return jsonify({"error": "Bạn đã vượt quá giới hạn số lần yêu cầu lịch sử giao dịch (5 lần). Vui lòng thử lại sau."}), 429

    result = fetch_transactions(wallet_address, limit=5)
    if isinstance(result, dict) and "error" in result:
        return jsonify(result), 400

    detailed_transactions = []
    for tx in result:
        tx_details = tx["full_data"]
        actions, token_info, total_value_usd = analyze_transaction_details(tx_details)
        detailed_transactions.append({
            "signature": tx["signature"],
            "actions": actions,
            "token_info": token_info,
            "total_value_usd": total_value_usd,
            "link": f"https://solscan.io/tx/{tx['signature']}"
        })

    # Include the last signature for pagination
    last_signature = detailed_transactions[-1]["signature"] if detailed_transactions else None
    return jsonify({
        "transactions": detailed_transactions,
        "last_signature": last_signature,
        "remaining_requests": MAX_HISTORY_REQUESTS - history_request_counts[wallet_address]
    })

@app.route('/more_transactions', methods=['POST'])
def more_transactions():
    data = request.get_json()
    wallet_address = data.get('wallet_address') if data else None
    last_signature = data.get('last_signature') if data else None
    if not wallet_address or not last_signature:
        return jsonify({"error": "Vui lòng cung cấp địa chỉ ví và chữ ký cuối cùng!"}), 400

    # Track the number of history requests
    if wallet_address not in history_request_counts:
        history_request_counts[wallet_address] = 0
    history_request_counts[wallet_address] += 1

    if history_request_counts[wallet_address] > MAX_HISTORY_REQUESTS:
        return jsonify({"error": "Bạn đã vượt quá giới hạn số lần yêu cầu lịch sử giao dịch (5 lần). Vui lòng thử lại sau."}), 429

    result = fetch_transactions(wallet_address, limit=5, before=last_signature)
    if isinstance(result, dict) and "error" in result:
        return jsonify(result), 400

    detailed_transactions = []
    for tx in result:
        tx_details = tx["full_data"]
        actions, token_info, total_value_usd = analyze_transaction_details(tx_details)
        detailed_transactions.append({
            "signature": tx["signature"],
            "actions": actions,
            "token_info": token_info,
            "total_value_usd": total_value_usd,
            "link": f"https://solscan.io/tx/{tx['signature']}"
        })

    last_signature = detailed_transactions[-1]["signature"] if detailed_transactions else None
    return jsonify({
        "transactions": detailed_transactions,
        "last_signature": last_signature,
        "remaining_requests": MAX_HISTORY_REQUESTS - history_request_counts[wallet_address]
    })

if __name__ == '__main__':
    from waitress import serve
    serve(app, host='0.0.0.0', port=5000)
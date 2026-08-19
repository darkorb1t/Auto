import sqlite3
import re
import threading
import requests
import asyncio
import time
import uuid

# API কনফিগারেশন
PRODSELLER_API_KEY = "psk_96d3db03478e51fa5ae114d428708206807ec88849249bf2"
PRODSELLER_BASE = "http://51.77.244.194/v1"
HEADERS = {"X-API-Key": PRODSELLER_API_KEY}

from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler, ConversationHandler, MessageHandler, filters

BOT_TOKEN = "8036869041:AAG0WtZkxJY1c8-_qW7itPy4V99hbayOi1k" # তোমার টোকেন দাও
ADMIN_ID = 6250222523
BKASH_NUMBER = "01611026722"
USD_RATE = 130

ASK_AMOUNT, ASK_TRXID = range(2)

flask_app = Flask(__name__)
bot_instance = Bot(token=BOT_TOKEN)

def init_db():
    conn = sqlite3.connect('bot_database.db')
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (telegram_id INTEGER PRIMARY KEY, balance REAL DEFAULT 0.0)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS transactions (trx_id TEXT PRIMARY KEY, telegram_id INTEGER, amount_usd REAL, amount_bdt REAL, status TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS products_config (product_id TEXT PRIMARY KEY, name TEXT, public_price REAL, commission REAL)''')
    # নতুন কলাম অ্যাড করার সেফটি চেক (যাতে ডাটাবেস ক্র্যাশ না করে)
    try:
        cursor.execute("ALTER TABLE products_config ADD COLUMN discount_price REAL")
        conn.commit()
    except sqlite3.OperationalError:
        pass # কলাম আগে থেকেই থাকলে স্কিপ করবে

    # নতুন টেবিল: যে SMS গুলো আগে আসবে সেগুলো সেভ রাখার জন্য
    cursor.execute('''CREATE TABLE IF NOT EXISTS received_sms (trx_id TEXT PRIMARY KEY, amount_bdt REAL)''')
    conn.commit()
    conn.close()

# ----------------- অ্যাডমিন প্যানেল -----------------
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # অ্যাডমিন ভেরিফিকেশন
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ আপনার এই কমান্ডটি ব্যবহার করার অনুমতি নেই।")
        return

    wait_msg = await update.message.reply_text("⏳ প্রোডাক্ট লিস্ট লোড হচ্ছে...")

    try:
        resp = requests.get(f"{PRODSELLER_BASE}/products", headers=HEADERS)
        if resp.status_code == 200:
            products = resp.json().get("products", [])
            if not products:
                await wait_msg.edit_text("দুঃখিত, হোস্ট API-তে কোনো প্রোডাক্ট নেই।")
                return

            keyboard = []
            for p in products:
                # অ্যাডমিনকে আসল দাম (Buy Price) দেখানো হচ্ছে
                btn_text = f"⚙️ {p['name']} (Buy: ${p['price']})"
                keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"admin_prod_{p['id']}")])

            reply_markup = InlineKeyboardMarkup(keyboard)
            await wait_msg.edit_text("👑 **অ্যাডমিন প্যানেল**\n\nপ্রাইস এবং কমিশন সেট করতে যেকোনো প্রোডাক্টে ক্লিক করুন:", parse_mode='Markdown', reply_markup=reply_markup)
        else:
            await wait_msg.edit_text("❌ হোস্ট API এর সাথে কানেক্ট করা যাচ্ছে না।")
    except Exception as e:
        await wait_msg.edit_text("❌ প্রোডাক্ট লোড করতে সমস্যা হয়েছে।")


# ----------------- বটের লজিক -----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = sqlite3.connect('bot_database.db')
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (telegram_id, balance) VALUES (?, ?)", (user_id, 0.0))
    conn.commit()
    conn.close()

    keyboard = [
        [InlineKeyboardButton("🛒 শপ (Shop)", callback_data='shop')],
        [InlineKeyboardButton("💰 ডিপোজিট (Deposit)", callback_data='deposit')],
        [InlineKeyboardButton("👤 আমার একাউন্ট (My Account)", callback_data='account')],
        [InlineKeyboardButton("📞 সাপোর্ট (Support)", callback_data='support')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("ডিজিটাল শপে স্বাগতম! 🛍️\n\nআপনার প্রয়োজনীয় সেবাটি বেছে নিন:", reply_markup=reply_markup)
    return ConversationHandler.END

async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    # --- ডাবল ক্লিক বা একসাথে ক্লিকের সমাধান (Debounce Logic) ---
    current_time = time.time()
    last_click = context.user_data.get('last_click_time', 0)
    
    # যদি ০.৫ সেকেন্ড বা তার কম সময়ের মধ্যে ক্লিক পড়ে, তবে পরের ক্লিকটা ইগনোর করবে
    if current_time - last_click <= 0.5:
        await query.answer("দয়া করে একটু অপেক্ষা করুন...", show_alert=False)
        return
        
    # নতুন ক্লিকের সময়টা সেভ করে রাখা
    context.user_data['last_click_time'] = current_time
    await query.answer()

    # --- আগের বাটন লজিকগুলো ---
    if query.data == 'deposit':
        keyboard = [[InlineKeyboardButton("❌ ক্যানসেল (Cancel)", callback_data='cancel_action')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        text = (
            "আপনি কত ডলার ($) ডিপোজিট করতে চান?\n"
            f"💱 আজকের রেট: ১$ = {USD_RATE} টাকা\n"
            "🔻 সর্বনিম্ন ডিপোজিট: ০.১$ (১৩ টাকা)\n\n"
            "💬 নিচে শুধু ডলারের পরিমাণটা লিখে মেসেজ করুন (যেমন: 1 বা 0.5):"
        )
        await query.edit_message_text(text, reply_markup=reply_markup)
        return ASK_AMOUNT
    
    elif query.data == 'account':
        user_id = update.effective_user.id
        conn = sqlite3.connect('bot_database.db', timeout=10)
        cursor = conn.cursor()
        cursor.execute("SELECT balance FROM users WHERE telegram_id = ?", (user_id,))
        balance = cursor.fetchone()[0]
        conn.close()
        
        keyboard = [[InlineKeyboardButton("🔙 ব্যাক (Back)", callback_data='start_menu')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(f"👤 আপনার বর্তমান ব্যালেন্স: ${balance}", reply_markup=reply_markup)
        return ConversationHandler.END
        
    elif query.data == 'support':
        keyboard = [[InlineKeyboardButton("🔙 ব্যাক (Back)", callback_data='start_menu')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("📞 সাপোর্টের জন্য অ্যাডমিনের সাথে যোগাযোগ করুন: @YourAdminID", reply_markup=reply_markup)
        return ConversationHandler.END
        
    elif query.data == 'shop':
        # সাইলেন্ট রেসপন্স দিয়ে এনিমেশন স্মুথ করা
        await query.answer(cache_time=0)
        await query.edit_message_text("🛒 শপের প্রোডাক্ট লোড হচ্ছে...")
        
        try:
            resp = requests.get(f"{PRODSELLER_BASE}/products", headers=HEADERS)
            if resp.status_code == 200:
                products = resp.json().get("products", [])
                if not products:
                    keyboard = [[InlineKeyboardButton("🔙 ব্যাক (Back)", callback_data='start_menu')]]
                    await query.edit_message_text("দুঃখিত, এই মুহূর্তে শপে কোনো প্রোডাক্ট নেই।", reply_markup=InlineKeyboardMarkup(keyboard))
                    return ConversationHandler.END
                    
                keyboard = []
                for p in products:
                    # লাইভ স্টক চেক করার লজিক
                    stock_info = ""
                    if 'stock' in p and p['stock'] is not None:
                        stock_info = f" | স্টক: {p['stock']}টি"
                    elif p.get('inStock'):
                        stock_info = " | ✅ ইন-স্টক"
                    else:
                        stock_info = " | ❌ স্টক আউট"
                        
                    btn_text = f"{p['name']} - ${p['price']}{stock_info}"
                    
                    # যদি আউট অফ স্টক হয়, তাহলে বাটনটা ডিজেবলড ফিল দেওয়ার জন্য callback_data চেঞ্জ করে দিতে পারো, 
                    # তবে আপাতত স্ট্যান্ডার্ড buy_{id} রাখছি।
                    keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"buy_{p['id']}")])
                    
                keyboard.append([InlineKeyboardButton("🔙 ব্যাক (Back)", callback_data='start_menu')])
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text("🛒 আমাদের শপের প্রোডাক্টগুলো নিচে দেওয়া হলো। কিনতে ক্লিক করুন:", reply_markup=reply_markup)
            else:
                keyboard = [[InlineKeyboardButton("🔙 ব্যাক (Back)", callback_data='start_menu')]]
                await query.edit_message_text("❌ হোস্ট API এর সাথে কানেক্ট করা যাচ্ছে না।", reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception as e:
            keyboard = [[InlineKeyboardButton("🔙 ব্যাক (Back)", callback_data='start_menu')]]
            await query.edit_message_text("❌ প্রোডাক্ট লোড করতে সমস্যা হয়েছে।", reply_markup=InlineKeyboardMarkup(keyboard))
            
        return ConversationHandler.END

        
    elif query.data == 'start_menu' or query.data == 'cancel_action':
        keyboard = [
            [InlineKeyboardButton("🛒 শপ (Shop)", callback_data='shop')],
            [InlineKeyboardButton("💰 ডিপোজিট (Deposit)", callback_data='deposit')],
            [InlineKeyboardButton("👤 আমার একাউন্ট (My Account)", callback_data='account')],
            [InlineKeyboardButton("📞 সাপোর্ট (Support)", callback_data='support')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("ডিজিটাল শপে স্বাগতম! 🛍️\n\nআপনার প্রয়োজনীয় সেবাটি বেছে নিন:", reply_markup=reply_markup)
        return ConversationHandler.END



async def process_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text
    
    # সব মেসেজের সাথে ক্যানসেল বাটন দেখানোর জন্য কীবোর্ড সেটআপ
    keyboard = [[InlineKeyboardButton("❌ ক্যানসেল (Cancel)", callback_data='cancel_action')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    try:
        usd_amount = float(user_input)
        
        if usd_amount < 0.1:
            await update.message.reply_text(
                "❌ সর্বনিম্ন ডিপোজিট ০.১$। দয়া করে সঠিক পরিমাণ লিখুন:", 
                reply_markup=reply_markup
            )
            return ASK_AMOUNT
            
        bdt_amount = round(usd_amount * USD_RATE, 2)
        
        # ইউজারের দেওয়া ডাটা সাময়িকভাবে সেভ রাখা TrxID চেকের জন্য
        context.user_data['pending_usd'] = usd_amount
        context.user_data['pending_bdt'] = bdt_amount
        
        text = (
            f"অনুগ্রহ করে নিচের পার্সোনাল বিকাশ নাম্বারে ঠিক **{bdt_amount} টাকা** সেন্ড মানি করুন।\n\n"
            f"📞 `{BKASH_NUMBER}`\n\n"
            f"টাকা পাঠানোর পর বিকাশ থেকে পাওয়া **Transaction ID (TrxID)** টি এখানে দিন।"
        )
        await update.message.reply_text(text, parse_mode='Markdown', reply_markup=reply_markup)
        return ASK_TRXID
        
    except ValueError:
        await update.message.reply_text(
            "❌ অনুগ্রহ করে শুধুমাত্র সংখ্যা লিখুন (যেমন: 1 বা 0.5):", 
            reply_markup=reply_markup
        )
        return ASK_AMOUNT

# --- ব্যাকগ্রাউন্ডে TrxID চেক করার স্মার্ট ফাংশন ---
async def check_transaction_background(bot, chat_id, trx_id, wait_msg_id):
    found = False
    
    # লুপ শুধু চেক করবে Webhook ডাটাবেস আপডেট করেছে কি না (completed/failed)
    for _ in range(12):
        await asyncio.sleep(5)
        
        conn = sqlite3.connect('bot_database.db', timeout=10)
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM transactions WHERE trx_id = ?", (trx_id,))
        result = cursor.fetchone()
        conn.close()
        
        if result and result[0] != 'pending':
            found = True
            break
            
    # ওয়েট মেসেজ ডিলিট করা
    try:
        await bot.delete_message(chat_id=chat_id, message_id=wait_msg_id)
    except:
        pass
        
    if not found:
        # ১ মিনিটেও SMS না পেলে pending থেকে denied করে দেওয়া
        conn = sqlite3.connect('bot_database.db', timeout=10)
        cursor = conn.cursor()
        cursor.execute("UPDATE transactions SET status = 'denied' WHERE trx_id = ?", (trx_id,))
        conn.commit()
        conn.close()
        
        await bot.send_message(chat_id=chat_id, text="❌ *রিকোয়েস্ট ডিনাইড!*\n১ মিনিট সময় পার হয়ে গেছে কিন্তু আমরা কোনো পেমেন্ট রিসিভ করিনি। সঠিক ট্রানজেকশন আইডি দিয়ে পুনরায় চেষ্টা করুন।", parse_mode='Markdown')
        
    # সবশেষে অটোমেটিক্যালি মেইন মেনু ওপেন করা
    keyboard = [
        [InlineKeyboardButton("🛒 শপ (Shop)", callback_data='shop')],
        [InlineKeyboardButton("💰 ডিপোজিট (Deposit)", callback_data='deposit')],
        [InlineKeyboardButton("👤 আমার একাউন্ট (My Account)", callback_data='account')],
        [InlineKeyboardButton("📞 সাপোর্ট (Support)", callback_data='support')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await bot.send_message(chat_id=chat_id, text="ডিজিটাল শপে স্বাগতম! 🛍️\n\nআপনার প্রয়োজনীয় সেবাটি বেছে নিন:", reply_markup=reply_markup)


async def process_trxid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # .upper() দেওয়া হলো যাতে ইউজার ছোট হাতের অক্ষর দিলেও কাজ করে
    trx_id = update.message.text.strip().upper() 
    pending_usd = context.user_data.get('pending_usd')
    pending_bdt = context.user_data.get('pending_bdt')
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    conn = sqlite3.connect('bot_database.db', timeout=10)
    cursor = conn.cursor()
    
    # ১. প্রথমেই চেক করবে SMS আগেই চলে এসেছে কি না
    cursor.execute("SELECT amount_bdt FROM received_sms WHERE trx_id = ?", (trx_id,))
    sms_result = cursor.fetchone()
    
    if sms_result:
        # SMS পাওয়া গেছে! লুপে যাওয়ার দরকারই নেই।
        sms_amount = sms_result[0]
        cursor.execute("DELETE FROM received_sms WHERE trx_id = ?", (trx_id,))
        
        if sms_amount == pending_bdt:
            cursor.execute("UPDATE users SET balance = balance + ? WHERE telegram_id = ?", (pending_usd, user_id))
            cursor.execute('''INSERT OR REPLACE INTO transactions (trx_id, telegram_id, amount_usd, amount_bdt, status) 
                              VALUES (?, ?, ?, ?, ?)''', (trx_id, user_id, pending_usd, pending_bdt, 'completed'))
            status_msg = f"🎉 পেমেন্ট সফল! আপনার একাউন্টে ${pending_usd} যোগ করা হয়েছে।"
        else:
            cursor.execute('''INSERT OR REPLACE INTO transactions (trx_id, telegram_id, amount_usd, amount_bdt, status) 
                              VALUES (?, ?, ?, ?, ?)''', (trx_id, user_id, pending_usd, pending_bdt, 'failed'))
            status_msg = "❌ *পেমেন্ট ভেরিফিকেশন ফেইলড!*\nআপনার দেওয়া Transaction ID অথবা টাকার পরিমাণ আমাদের রেকর্ড অনুযায়ী সঠিক নয়। পুনরায় চেষ্টা করুন।"
            
        conn.commit()
        conn.close()
        
        await update.message.reply_text(status_msg, parse_mode='Markdown')
        
        # সাথে সাথে মেনু দিয়ে দেওয়া
        keyboard = [
            [InlineKeyboardButton("🛒 শপ (Shop)", callback_data='shop')],
            [InlineKeyboardButton("💰 ডিপোজিট (Deposit)", callback_data='deposit')],
            [InlineKeyboardButton("👤 আমার একাউন্ট (My Account)", callback_data='account')],
            [InlineKeyboardButton("📞 সাপোর্ট (Support)", callback_data='support')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("ডিজিটাল শপে স্বাগতম! 🛍️\n\nআপনার প্রয়োজনীয় সেবাটি বেছে নিন:", reply_markup=reply_markup)
        
        return ConversationHandler.END
        
    else:
        # SMS এখনো আসেনি। pending হিসেবে সেভ করে লুপে পাঠাবো
        cursor.execute('''INSERT OR REPLACE INTO transactions (trx_id, telegram_id, amount_usd, amount_bdt, status) 
                          VALUES (?, ?, ?, ?, ?)''', (trx_id, user_id, pending_usd, pending_bdt, 'pending'))
        conn.commit()
        conn.close()
        
        wait_keyboard = [[InlineKeyboardButton("🔙 মেইন মেনু (Main Menu)", callback_data='start_menu')]]
        wait_msg = await update.message.reply_text("⏳ আপনার ট্রানজেকশন ব্যাকগ্রাউন্ডে চেক করা হচ্ছে... (সর্বোচ্চ ১ মিনিট)\n\n*এই সময়ে আপনি বটের অন্যান্য মেনু ব্যবহার করতে পারেন।*", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(wait_keyboard))
        
        asyncio.create_task(
            check_transaction_background(
                context.bot, chat_id, trx_id, wait_msg.message_id
            )
        )
        
        return ConversationHandler.END



async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("প্রসেস বাতিল করা হয়েছে।")
    return ConversationHandler.END

async def process_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    # --- ডাবল ক্লিক বা একসাথে ক্লিকের সমাধান (Debounce Logic) ---
    current_time = time.time()
    last_click = context.user_data.get('last_click_time', 0)
    
    if current_time - last_click <= 0.5:
        await query.answer("অর্ডার প্রসেস হচ্ছে, একটু অপেক্ষা করুন...", show_alert=False)
        return
        
    context.user_data['last_click_time'] = current_time
    await query.answer()

    
    product_id = query.data.split('_')[1] # callback_data "buy_64abc..." থেকে আইডি বের করা
    user_id = update.effective_user.id
    
    await query.edit_message_text("অর্ডার প্রসেস হচ্ছে, অনুগ্রহ করে অপেক্ষা করুন... ⏳")
    
    try:
        # ১. প্রোডাক্টের দাম ও নাম হোস্ট API থেকে চেক করা
        prod_resp = requests.get(f"{PRODSELLER_BASE}/products/{product_id}", headers=HEADERS)
        if prod_resp.status_code != 200:
            await query.edit_message_text("❌ প্রোডাক্টের বিস্তারিত জানতে সমস্যা হচ্ছে।")
            return
        
        product_data = prod_resp.json()
        product_price = float(product_data['price'])
        product_name = product_data['name']
        
    except Exception as e:
        await query.edit_message_text("❌ API এরর।")
        return
        
    # ২. ইউজারের ব্যালেন্স চেক করা
    conn = sqlite3.connect('bot_database.db')
    cursor = conn.cursor()
    cursor.execute("SELECT balance FROM users WHERE telegram_id = ?", (user_id,))
    user_balance = cursor.fetchone()[0]
    
    if user_balance < product_price:
        conn.close()
        await query.edit_message_text(f"❌ আপনার ব্যালেন্স অপর্যাপ্ত।\n\nপ্রোডাক্টের দাম: ${product_price}\nআপনার ব্যালেন্স: ${user_balance}\n\nদয়া করে মেনু থেকে ডিপোজিট করুন।")
        return
        
    # ৩. ব্যালেন্স কাটা এবং API তে অর্ডার করা
    new_balance = round(user_balance - product_price, 2)
    idem_key = str(uuid.uuid4()) # ডাবল চার্জ এড়াতে ইউনিক আইডি
    
    order_headers = {
        "X-API-Key": PRODSELLER_API_KEY,
        "Idempotency-Key": idem_key
    }
    order_payload = {"productId": product_id, "quantity": 1}
    
    order_resp = requests.post(f"{PRODSELLER_BASE}/orders", headers=order_headers, json=order_payload)
    
    if order_resp.status_code == 200:
        order_data = order_resp.json()
        
        if "error" in order_data:
            await query.edit_message_text(f"❌ হোস্ট API এরর: {order_data['error']}")
        else:
            # অর্ডার সাকসেস! ব্যালেন্স আপডেট করা
            cursor.execute("UPDATE users SET balance = ? WHERE telegram_id = ?", (new_balance, user_id))
            conn.commit()
            
            key = order_data.get("deliveredKey", "(pending delivery)")
            order_id = order_data.get("orderId", "N/A")[:8]
            
            success_msg = (
                f"✅ **অর্ডার সফল হয়েছে!**\n\n"
                f"🛒 প্রোডাক্ট: {product_name}\n"
                f"💰 দাম: ${product_price}\n"
                f"💳 বর্তমান ব্যালেন্স: ${new_balance}\n"
                f"🧾 অর্ডার আইডি: #{order_id}\n\n"
                f"🔑 **আপনার ডেলিভারি কি (Key):**\n`{key}`"
            )
            await query.edit_message_text(success_msg, parse_mode='Markdown')
    else:
        await query.edit_message_text(f"❌ অর্ডার ফেইল্ড (Status: {order_resp.status_code})")
        
    conn.close()


# ----------------- Webhook (Flask App) -----------------

@flask_app.route('/bkash-webhook', methods=['POST'])
def bkash_webhook():
    data = request.json
    if not data or 'message' not in data:
        return "No message found", 400
        
    sms_text = data['message']
    
    trx_match = re.search(r'TrxID\s([A-Z0-9]+)', sms_text)
    amount_match = re.search(r'Tk\s([0-9.]+)', sms_text)
    
    if trx_match and amount_match:
        sms_trx_id = trx_match.group(1)
        sms_amount = float(amount_match.group(1))
        
        conn = sqlite3.connect('bot_database.db', timeout=10)
        cursor = conn.cursor()
        
        cursor.execute("SELECT telegram_id, amount_usd, amount_bdt FROM transactions WHERE trx_id = ? AND status = 'pending'", (sms_trx_id,))
        result = cursor.fetchone()
        
        if result:
            user_id, amount_usd, amount_bdt = result
            if sms_amount == amount_bdt:
                cursor.execute("UPDATE users SET balance = balance + ? WHERE telegram_id = ?", (amount_usd, user_id))
                cursor.execute("UPDATE transactions SET status = 'completed' WHERE trx_id = ?", (sms_trx_id,))
                conn.commit()
                requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                    "chat_id": user_id, "text": f"🎉 পেমেন্ট অটো-ভেরিফাই হয়েছে! আপনার একাউন্টে ${amount_usd} যোগ করা হয়েছে।"
                })
            else:
                cursor.execute("UPDATE transactions SET status = 'failed' WHERE trx_id = ?", (sms_trx_id,))
                conn.commit()
                # --- সিকিউরিটি আপডেট করা মেসেজ ---
                requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                    "chat_id": user_id, 
                    "text": "❌ *পেমেন্ট ভেরিফিকেশন ফেইলড!*\nআপনার দেওয়া Transaction ID অথবা টাকার পরিমাণ আমাদের রেকর্ড অনুযায়ী সঠিক নয়। দয়া করে সঠিক ট্রানজেকশন আইডি এবং সঠিক পরিমাণ টাকা দিয়ে পুনরায় নতুন করে ডিপোজিট রিকোয়েস্ট দিন।",
                    "parse_mode": "Markdown"
                })
        else:
            cursor.execute("INSERT OR IGNORE INTO received_sms (trx_id, amount_bdt) VALUES (?, ?)", (sms_trx_id, sms_amount))
            conn.commit()
        
        conn.close()
        
    return "OK", 200


def run_flask():
    flask_app.run(host='0.0.0.0', port=5000, use_reloader=False)

if __name__ == '__main__':
    init_db()
    
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    
    print("System initialized by darkorb1t...")
    print("Dual-Check Webhook is running on port 5000")
    
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_click)],
        states={
            ASK_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_amount),
                CallbackQueryHandler(button_click, pattern='^cancel_action$') # ক্যানসেল বাটন হ্যান্ডেল করা
            ],
            ASK_TRXID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_trxid),
                CallbackQueryHandler(button_click, pattern='^cancel_action$') # ক্যানসেল বাটন হ্যান্ডেল করা
            ],
        },
        fallbacks=[CommandHandler('cancel', cancel), CommandHandler('start', start)]
    )

    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(process_purchase, pattern='^buy_'))

 
    print("Bot is running! Press Ctrl+C to stop.")
    app.run_polling()


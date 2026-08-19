import sqlite3
import re
import threading
import requests
import io
import asyncio
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
import time
import uuid

# API কনফিগারেশন
PRODSELLER_API_KEY = "psk_96d3db03478e51fa5ae114d428708206807ec88849249bf2"
PRODSELLER_BASE = "https://51.77.244.194/v1"
HEADERS = {"X-API-Key": PRODSELLER_API_KEY}

from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler, ConversationHandler, MessageHandler, filters

BOT_TOKEN = "8036869041:AAG0WtZkxJY1c8-_qW7itPy4V99hbayOi1k" # তোমার টোকেন দাও
ADMIN_ID = 6250222523
BKASH_NUMBER = "01611026722"
USD_RATE = 130

ASK_AMOUNT, ASK_TRXID = range(2)
ASK_PUBLIC_PRICE, ASK_COMMISSION, ASK_DISCOUNT_PRICE, ASK_QUANTITY = range(2, 6)


flask_app = Flask(__name__)
bot_instance = Bot(token=BOT_TOKEN)

def init_db():
    conn = sqlite3.connect('bot_database.db')
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (telegram_id INTEGER PRIMARY KEY, balance REAL DEFAULT 0.0)''')
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
    # রেফারেল ট্র্যাক করার জন্য নতুন কলাম
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER")
        cursor.execute("ALTER TABLE users ADD COLUMN total_purchases INTEGER DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass # কলাম আগে থেকেই থাকলে স্কিপ করবে

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
        resp = requests.get(f"{PRODSELLER_BASE}/products", headers=HEADERS, verify=False, timeout=10)
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
            await wait_msg.edit_text("👑 **অ্যাডমিন প্যানেল**\n\nপাবলিক প্রাইস, কমিশন এবং ডিসকাউন্ট সেট করতে যেকোনো প্রোডাক্টে ক্লিক করুন:", parse_mode='Markdown', reply_markup=reply_markup)

        else:
            await wait_msg.edit_text("❌ হোস্ট API এর সাথে কানেক্ট করা যাচ্ছে না।")
    except Exception as e:
        await wait_msg.edit_text("❌ প্রোডাক্ট লোড করতে সমস্যা হয়েছে।")

async def admin_product_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    
    if user_id != ADMIN_ID:
        await query.answer("❌ আপনি অ্যাডমিন নন!", show_alert=True)
        return
        
    await query.answer()
    
    product_id = query.data.split('admin_prod_')[1]
    context.user_data['admin_product_id'] = product_id
    
    cancel_kb = [[InlineKeyboardButton("❌ ক্যানসেল (Cancel)", callback_data='admin_cancel')]]
    
    # edit_message_text এর বদলে reply_text ব্যবহার করা হলো, যাতে প্রোডাক্ট লিস্ট উপরে থেকে যায়
    await query.message.reply_text(
        f"⚙️ **প্রোডাক্ট কনফিগারেশন**\n"
        f"🆔 প্রোডাক্ট আইডি: `{product_id}`\n\n"
        "১. প্রথমে এই প্রোডাক্টের **Public Price** (সাধারণ ইউজারের জন্য দাম) কত ডলার হবে তা লিখুন (যেমন: 2.50):", 
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(cancel_kb)
    )
    return ASK_PUBLIC_PRICE

async def process_public_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancel_kb = [[InlineKeyboardButton("❌ ক্যানসেল (Cancel)", callback_data='admin_cancel')]]
    try:
        price = float(update.message.text)
        context.user_data['admin_public_price'] = price
        await update.message.reply_text(
            f"✅ পাবলিক প্রাইস সেট হলো: ${price}\n\n"
            "২. এবার **Commission** (রেফারার কত ডলার বোনাস পাবে) তা লিখুন (যেমন: 0.50):", 
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(cancel_kb)
        )
        return ASK_COMMISSION
    except ValueError:
        await update.message.reply_text("❌ অনুগ্রহ করে শুধুমাত্র সংখ্যা লিখুন (যেমন: 2.50):", reply_markup=InlineKeyboardMarkup(cancel_kb))
        return ASK_PUBLIC_PRICE

async def process_commission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancel_kb = [[InlineKeyboardButton("❌ ক্যানসেল (Cancel)", callback_data='admin_cancel')]]
    try:
        comm = float(update.message.text)
        context.user_data['admin_commission'] = comm
        await update.message.reply_text(
            f"✅ কমিশন সেট হলো: ${comm}\n\n"
            "৩. সবশেষে **Discount Price** (৫+ রেফার করা ইউজারের জন্য স্পেশাল দাম) লিখুন (যেমন: 2.00):", 
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(cancel_kb)
        )
        return ASK_DISCOUNT_PRICE
    except ValueError:
        await update.message.reply_text("❌ অনুগ্রহ করে শুধুমাত্র সংখ্যা লিখুন:", reply_markup=InlineKeyboardMarkup(cancel_kb))
        return ASK_COMMISSION

async def process_discount_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        disc = float(update.message.text)
        product_id = context.user_data['admin_product_id']
        public_price = context.user_data['admin_public_price']
        commission = context.user_data['admin_commission']
        
        conn = sqlite3.connect('bot_database.db', timeout=10)
        cursor = conn.cursor()
        cursor.execute('''INSERT OR REPLACE INTO products_config (product_id, name, public_price, commission, discount_price) 
                          VALUES (?, ?, ?, ?, ?)''', (product_id, "Configured Product", public_price, commission, disc))
        conn.commit()
        conn.close()
        
        await update.message.reply_text(
            f"🎉 চমৎকার! প্রোডাক্টের কনফিগারেশন সেভ হয়েছে।\n"
            f"💰 পাবলিক প্রাইস: ${public_price} | 🎁 কমিশন: ${commission} | 🔥 ডিসকাউন্ট: ${disc}"
        )
        
        # অটোমেটিক আবার প্যানেল ওপেন করে দেওয়া
        await admin_panel(update, context)
        return ConversationHandler.END
        
    except ValueError:
        cancel_kb = [[InlineKeyboardButton("❌ ক্যানসেল (Cancel)", callback_data='admin_cancel')]]
        await update.message.reply_text("❌ অনুগ্রহ করে শুধুমাত্র সংখ্যা লিখুন:", reply_markup=InlineKeyboardMarkup(cancel_kb))
        return ASK_DISCOUNT_PRICE

# ক্যানসেল বাটন হ্যান্ডেল করার ফাংশন
async def admin_cancel_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("❌ কনফিগারেশন বাতিল করা হয়েছে। আপনি চাইলে উপরের লিস্ট থেকে আবার অন্য প্রোডাক্ট সিলেক্ট করতে পারেন।")
    return ConversationHandler.END

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # শুধুমাত্র অ্যাডমিন ব্রডকাস্ট করতে পারবে
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ আপনার এই কমান্ডটি ব্যবহার করার অনুমতি নেই।")
        return

    # কমান্ডের সাথে মেসেজ দেওয়া হয়েছে কি না চেক করা (যেমন: /broadcast আসসালামু আলাইকুম)
    if not context.args:
        await update.message.reply_text("⚠️ দয়া করে মেসেজ লিখে দিন।\nউদাহরণ: `/broadcast আপনার মেসেজ এখানে লিখুন`", parse_mode='Markdown')
        return

    broadcast_text = " ".join(context.args)
    
    conn = sqlite3.connect('bot_database.db', timeout=10)
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id FROM users")
    all_users = cursor.fetchall()
    conn.close()

    success_count = 0
    fail_count = 0

    status_msg = await update.message.reply_text("⏳ ব্রডকাস্ট পাঠানো শুরু হয়েছে, দয়া করে অপেক্ষা করুন...")

    for row in all_users:
        uid = row[0]
        try:
            await context.bot.send_message(chat_id=uid, text=broadcast_text, parse_mode='Markdown')
            success_count += 1
            await asyncio.sleep(0.1) # টেলিগ্রামের ফ্লাড লিমিট এড়ানোর জন্য ছোট ডিলে
        except Exception:
            fail_count += 1

    await status_msg.edit_text(
        f"✅ **ব্রডকাস্ট সম্পন্ন হয়েছে!**\n\n"
        f"📩 সফলভাবে পাঠানো হয়েছে: {success_count} জন\n"
        f"❌ ফেইল হয়েছে (বট ব্লক করা): {fail_count} জন",
        parse_mode='Markdown'
    )


# ----------------- বটের লজিক -----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args # রেফারেল লিংক থেকে আইডি ধরার জন্য
    
    conn = sqlite3.connect('bot_database.db', timeout=10)
    cursor = conn.cursor()
    
    # চেক করবো ইউজার নতুন কি না
    cursor.execute("SELECT telegram_id FROM users WHERE telegram_id = ?", (user_id,))
    user_exists = cursor.fetchone()
    
    if not user_exists:
        referrer_id = None
        # যদি লিংক দিয়ে জয়েন করে এবং নিজের লিংক নিজে না হয়
        if args and args[0].isdigit():
            ref_id = int(args[0])
            if ref_id != user_id:
                referrer_id = ref_id
                
        cursor.execute("INSERT INTO users (telegram_id, balance, referred_by, total_purchases) VALUES (?, ?, ?, ?)", (user_id, 0.0, referrer_id, 0))
        conn.commit()
    
    conn.close()

    # মেইন মেনুতে রেফার বাটন অ্যাড করা হলো
    keyboard = [
        [InlineKeyboardButton("🛒 শপ (Shop)", callback_data='shop')],
        [InlineKeyboardButton("💰 ডিপোজিট (Deposit)", callback_data='deposit')],
        [InlineKeyboardButton("👤 আমার একাউন্ট (My Account)", callback_data='account')],
        [InlineKeyboardButton("🎁 রেফার এন্ড আর্ন (Refer & Earn)", callback_data='refer')],
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

    elif query.data == 'refer':
        user_id = update.effective_user.id
        bot_username = context.bot.username
        
        # ইউজারের ইউনিক রেফারেল লিংক
        referral_link = f"https://t.me/{bot_username}?start={user_id}"
        
        text = (
            "🎁 **রেফার এন্ড আর্ন (Refer & Earn)**\n\n"
            "বটটি আপনার বন্ধুদের সাথে শেয়ার করে জিতে নিন কমিশন এবং লাইফটাইম ডিসকাউন্ট!\n\n"
            "📌 **শর্তাবলী:**\n"
            "১. **আপনার যোগ্যতা:** রেফারেল সুবিধা উপভোগ করতে আপনাকে আমাদের শপ থেকে অন্তত **২টি প্রোডাক্ট** কিনতে হবে।\n"
            "২. **সাকসেসফুল রেফার:** আপনার লিংকে জয়েন করার পর বন্ধুটি অন্তত **১টি প্রোডাক্ট** কিনলে সেটি 'সাকসেসফুল রেফার' হিসেবে কাউন্ট হবে।\n"
            "৩. **ডিসকাউন্ট সুবিধা:** ৫টি সাকসেসফুল রেফার পূর্ণ হলে আপনি শপের প্রোডাক্টগুলোতে স্পেশাল **Discount Price**-এ কেনার সুযোগ পাবেন!\n"
            "৪. **লাইফটাইম কমিশন:** আপনার রেফার করা ইউজাররা এরপর থেকে যতবার প্রোডাক্ট কিনবে, আপনি নির্দিষ্ট পরিমাণ **কমিশন** পাবেন!\n\n"
            "⚠️ **বিশেষ নোট:** সব প্রোডাক্টে কমিশন থাকে না (যেমন: খুব কম দামের প্রোডাক্টে কমিশন $0)। অ্যাডমিন যে প্রোডাক্টে যতটুকু কমিশন সেট করেছেন, আপনি শুধু সেটুকুই পাবেন।\n\n"
            f"🔗 **আপনার রেফারেল লিংক (কপি করতে ট্যাপ করুন):**\n`{referral_link}`"
        )
        
        keyboard = [[InlineKeyboardButton("🔙 ব্যাক (Back)", callback_data='start_menu')]]
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)
        return ConversationHandler.END
 
    
    elif query.data == 'support':
        keyboard = [[InlineKeyboardButton("🔙 ব্যাক (Back)", callback_data='start_menu')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("📞 সাপোর্টের জন্য অ্যাডমিনের সাথে যোগাযোগ করুন: @darkorb1t", reply_markup=reply_markup)
        return ConversationHandler.END
        
    elif query.data == 'shop':
        # সাইলেন্ট রেসপন্স দিয়ে এনিমেশন স্মুথ করা
        await query.answer(cache_time=0)
        await query.edit_message_text("🛒 শপের প্রোডাক্ট লোড হচ্ছে...")
        
        try:
            resp = requests.get(f"{PRODSELLER_BASE}/products", headers=HEADERS, verify=False, timeout=10)
            if resp.status_code == 200:
                products = resp.json().get("products", [])
                if not products:
                    keyboard = [[InlineKeyboardButton("🔙 ব্যাক (Back)", callback_data='start_menu')]]
                    await query.edit_message_text("দুঃখিত, এই মুহূর্তে শপে কোনো প্রোডাক্ট নেই।", reply_markup=InlineKeyboardMarkup(keyboard))
                    return ConversationHandler.END
                
                # --- নতুন লজিক: ডাটাবেস থেকে কাস্টম প্রাইস আনা ---
                conn = sqlite3.connect('bot_database.db', timeout=10)
                cursor = conn.cursor()
                cursor.execute("SELECT product_id, public_price FROM products_config")
                config_data = cursor.fetchall()
                conn.close()
                
                # Dictionary তৈরি করা সহজে খোঁজার জন্য
                price_map = {row[0]: row[1] for row in config_data}
                # -------------------------------------------------
                    
                keyboard = []
                for p in products:
                    # যদি ডাটাবেসে কাস্টম প্রাইস থাকে সেটা নিবে, না থাকলে API এর কেনা দামটাই দেখাবে
                    display_price = price_map.get(p['id'], p['price'])
                    
                    # লাইভ স্টক চেক করার লজিক
                    stock_info = ""
                    if 'stock' in p and p['stock'] is not None:
                        stock_info = f" | স্টক: {p['stock']}টি"
                    elif p.get('inStock'):
                        stock_info = " | ✅ ইন-স্টক"
                    else:
                        stock_info = " | ❌ স্টক আউট"
                        
                    btn_text = f"{p['name']} - ${display_price}{stock_info}"
                    
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
            [InlineKeyboardButton("🎁 রেফার এন্ড আর্ন (Refer & Earn)", callback_data='refer')],
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
        
        # ইউজার ইনপুটের সমান বা বেশি টাকা পাঠালে অ্যাক্সেপ্ট হবে
        if sms_amount >= pending_bdt:
            actual_usd = round(sms_amount / USD_RATE, 2) # আসল পাঠানো টাকার হিসাব
            cursor.execute("UPDATE users SET balance = balance + ? WHERE telegram_id = ?", (actual_usd, user_id))
            cursor.execute('''INSERT OR REPLACE INTO transactions (trx_id, telegram_id, amount_usd, amount_bdt, status) 
                              VALUES (?, ?, ?, ?, ?)''', (trx_id, user_id, actual_usd, sms_amount, 'completed'))
            status_msg = f"🎉 পেমেন্ট সফল! আপনার পাঠানো {sms_amount} টাকার বিনিময়ে একাউন্টে **${actual_usd}** যোগ করা হয়েছে।"
        else:
            cursor.execute('''INSERT OR REPLACE INTO transactions (trx_id, telegram_id, amount_usd, amount_bdt, status) 
                              VALUES (?, ?, ?, ?, ?)''', (trx_id, user_id, pending_usd, pending_bdt, 'failed'))
            status_msg = f"❌ *পেমেন্ট ফেইলড!*\nআপনি রিকোয়েস্ট করেছেন {pending_bdt} টাকার, কিন্তু ট্রানজেকশনে পাওয়া গেছে {sms_amount} টাকা। টাকার পরিমাণ কম হওয়ায় এটি বাতিল করা হয়েছে।"
            
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

async def process_purchase_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    current_time = time.time()
    last_click = context.user_data.get('last_click_time', 0)
    
    if current_time - last_click <= 0.5:
        await query.answer("অপেক্ষা করুন...", show_alert=False)
        return
        
    context.user_data['last_click_time'] = current_time
    await query.answer()

    product_id = query.data.split('_')[1]
    context.user_data['buy_product_id'] = product_id
    
    await query.edit_message_text("প্রোডাক্টের বিস্তারিত লোড হচ্ছে... ⏳")
    
    try:
        # API থেকে প্রোডাক্টের বিস্তারিত তথ্য আনা
        prod_resp = requests.get(f"{PRODSELLER_BASE}/products/{product_id}", headers=HEADERS, verify=False, timeout=10)
        if prod_resp.status_code != 200:
            await query.edit_message_text("❌ প্রোডাক্টের বিস্তারিত জানতে সমস্যা হচ্ছে।")
            return ConversationHandler.END
            
        product_data = prod_resp.json()
        product_name = product_data.get('name', 'Unknown Product')
        # ডেসক্রিপশন না থাকলে ডিফল্ট মেসেজ
        description = product_data.get('description', 'কোনো ডেসক্রিপশন পাওয়া যায়নি।')
        api_buy_price = float(product_data.get('price', 0))
    except Exception:
        await query.edit_message_text("❌ API এরর।")
        return ConversationHandler.END

    conn = sqlite3.connect('bot_database.db', timeout=10)
    cursor = conn.cursor()
    cursor.execute("SELECT public_price FROM products_config WHERE product_id = ?", (product_id,))
    db_res = cursor.fetchone()
    conn.close()
    
    display_price = float(db_res[0]) if db_res else api_buy_price

    keyboard = [[InlineKeyboardButton("❌ ক্যানসেল (Cancel)", callback_data='cancel_action')]]
    
    # HTML পার্স মোড ব্যবহার করা হলো যাতে ডেসক্রিপশনের ভেতরের লেখায় এরর না আসে
    msg_text = (
        f"🛍️ <b>{product_name}</b>\n\n"
        f"{description}\n\n"
        f"💵 <b>Price:</b> ${display_price}\n"
        f"➖➖➖➖➖➖➖➖➖\n"
        f"🛒 <b>আপনি এই প্রোডাক্টটি কত পিস নিতে চান?</b>\n"
        f"দয়া করে নিচে শুধু সংখ্যাটি লিখে মেসেজ করুন (যেমন: 1, 2, 5):"
    )
    
    await query.edit_message_text(msg_text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    return ASK_QUANTITY


async def process_purchase_final(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text
    keyboard = [[InlineKeyboardButton("❌ ক্যানসেল (Cancel)", callback_data='cancel_action')]]
    
    try:
        quantity = int(user_input)
        if quantity < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ অনুগ্রহ করে সঠিক সংখ্যা দিন (যেমন: 1, 2, 5):", reply_markup=InlineKeyboardMarkup(keyboard))
        return ASK_QUANTITY
        
    product_id = context.user_data.get('buy_product_id')
    user_id = update.effective_user.id
    
    wait_msg = await update.message.reply_text("অর্ডার প্রসেস হচ্ছে, অনুগ্রহ করে অপেক্ষা করুন... ⏳")
    
    try:
        prod_resp = requests.get(f"{PRODSELLER_BASE}/products/{product_id}", headers=HEADERS, verify=False)
        if prod_resp.status_code != 200:
            await wait_msg.edit_text("❌ প্রোডাক্টের বিস্তারিত জানতে সমস্যা হচ্ছে।")
            return ConversationHandler.END
        
        product_data = prod_resp.json()
        api_buy_price = float(product_data['price'])
        product_name = product_data['name']
    except Exception as e:
        await wait_msg.edit_text("❌ API এরর।")
        return ConversationHandler.END
        
    conn = sqlite3.connect('bot_database.db', timeout=10)
    cursor = conn.cursor()
    
    cursor.execute("SELECT public_price, commission, discount_price FROM products_config WHERE product_id = ?", (product_id,))
    db_res = cursor.fetchone()
    
    public_price = float(db_res[0]) if db_res else api_buy_price
    commission = float(db_res[1]) if db_res and db_res[1] else 0.0
    discount_price = float(db_res[2]) if db_res and db_res[2] else public_price
    
    cursor.execute("SELECT balance, referred_by, total_purchases FROM users WHERE telegram_id = ?", (user_id,))
    user_data = cursor.fetchone()
    user_balance = user_data[0]
    referred_by = user_data[1]
    total_purchases = user_data[2]
    
    # ১ পিসের দাম হিসাব
    single_unit_price = public_price
    if total_purchases >= 2:
        cursor.execute("SELECT COUNT(*) FROM users WHERE referred_by = ? AND total_purchases >= 1", (user_id,))
        if cursor.fetchone()[0] >= 5:
            single_unit_price = discount_price
            
    # মোট দাম হিসাব (Quantity দিয়ে গুণ)
    final_price = round(single_unit_price * quantity, 2)
    
    if user_balance < final_price:
        conn.close()
        await wait_msg.edit_text(f"❌ আপনার ব্যালেন্স অপর্যাপ্ত।\n\nমোট দাম: ${final_price} ({quantity} পিস)\nআপনার ব্যালেন্স: ${user_balance}\n\nদয়া করে মেনু থেকে ডিপোজিট করুন।")
        return ConversationHandler.END
        
    new_balance = round(user_balance - final_price, 2)
    cursor.execute("UPDATE users SET balance = ? WHERE telegram_id = ?", (new_balance, user_id))
    conn.commit()
    
    idem_key = str(uuid.uuid4())
    order_headers = {"X-API-Key": PRODSELLER_API_KEY, "Idempotency-Key": idem_key}
    order_payload = {"productId": product_id, "quantity": quantity}
    
    order_resp = requests.post(f"{PRODSELLER_BASE}/orders", headers=order_headers, json=order_payload, verify=False, timeout=10)
    
    try:
        order_data = order_resp.json()
    except Exception:
        order_data = {"error": f"Invalid response (Status: {order_resp.status_code})"}

    if "error" not in order_data:
        cursor.execute("UPDATE users SET total_purchases = total_purchases + 1 WHERE telegram_id = ?", (user_id,))
        conn.commit()
        
        # রেফারেল কমিশন (Quantity অনুযায়ী গুণ হবে)
        total_commission = commission * quantity
        if referred_by and total_commission > 0:
            cursor.execute("SELECT total_purchases FROM users WHERE telegram_id = ?", (referred_by,))
            referrer_data = cursor.fetchone()
            if referrer_data and referrer_data[0] >= 2:
                cursor.execute("UPDATE users SET balance = balance + ? WHERE telegram_id = ?", (total_commission, referred_by))
                conn.commit()
                try:
                    await context.bot.send_message(chat_id=referred_by, text=f"🎉 **রেফারেল বোনাস!**\nআপনার রেফার করা ইউজার {quantity}টি প্রোডাক্ট কিনেছেন। আপনি **${total_commission}** কমিশন পেয়েছেন!", parse_mode='Markdown')
                except:
                    pass 
                    
        # API থেকে Keys রিসিভ করা (একাধিক হলে list)
        keys = order_data.get("deliveredKeys")
        if not keys:
            single_key = order_data.get("deliveredKey", "(pending delivery)")
            keys = [single_key]
            
        order_id = order_data.get("orderId", "N/A")[:8]
        
        price_msg = f"💰 মোট দাম: ${final_price} (স্পেশাল ডিসকাউন্ট!)" if single_unit_price < public_price else f"💰 মোট দাম: ${final_price}"
        
        success_msg = (
            f"✅ **অর্ডার সফল হয়েছে!**\n\n"
            f"🛒 প্রোডাক্ট: {product_name} ({quantity} পিস)\n"
            f"{price_msg}\n"
            f"💳 বর্তমান ব্যালেন্স: ${new_balance}\n"
            f"🧾 অর্ডার আইডি: #{order_id}\n\n"
            f"📁 **আপনার {quantity}টি প্রোডাক্টের ফাইল নিচে দেওয়া হলো 👇**"
        )
        await wait_msg.edit_text(success_msg, parse_mode='Markdown')
        
        if keys and keys[0] != "(pending delivery)":
            # একাধিক Key থাকলে লাইন ব্রেক দিয়ে সুন্দর করে সাজানো
            file_text = "\n\n========================\n\n".join(keys)
            file_content = file_text.encode('utf-8')
            document = io.BytesIO(file_content)
            document.name = f"Darkorb1t_Order_{order_id}_{quantity}pcs.txt"
            await context.bot.send_document(chat_id=user_id, document=document)
            
    else:
        cursor.execute("UPDATE users SET balance = balance + ? WHERE telegram_id = ?", (final_price, user_id))
        conn.commit()
        await wait_msg.edit_text(f"❌ অর্ডার ফেইল্ড: {order_data.get('error', 'Unknown Error')}\n*(আপনার ব্যালেন্স রিফান্ড করা হয়েছে)*", parse_mode='Markdown')
        
    conn.close()
    return ConversationHandler.END




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
            if sms_amount >= amount_bdt:
                actual_usd = round(sms_amount / USD_RATE, 2)
                cursor.execute("UPDATE users SET balance = balance + ? WHERE telegram_id = ?", (actual_usd, user_id))
                # ট্রানজেকশন টেবিলে আসল ডলার এবং টাকার পরিমাণ আপডেট করে দেওয়া হলো
                cursor.execute("UPDATE transactions SET status = 'completed', amount_usd = ?, amount_bdt = ? WHERE trx_id = ?", (actual_usd, sms_amount, sms_trx_id))
                conn.commit()
                requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                    "chat_id": user_id, 
                    "text": f"🎉 পেমেন্ট অটো-ভেরিফাই হয়েছে!...",
                    "parse_mode": "Markdown"
                }, timeout=10)


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
    
    # ----------------- সংশোধিত মাস্টার হ্যান্ডলার -----------------
    master_conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(button_click, pattern='^(deposit|account|support|shop|refer|start_menu|cancel_action)$'),
            CallbackQueryHandler(process_purchase_start, pattern='^buy_'),
            CallbackQueryHandler(admin_product_click, pattern='^admin_prod_')
        ],
        states={
            ASK_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_amount),
                CallbackQueryHandler(button_click, pattern='^cancel_action$')
            ],
            ASK_TRXID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_trxid),
                CallbackQueryHandler(button_click, pattern='^cancel_action$')
            ],
            ASK_QUANTITY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_purchase_final),
                CallbackQueryHandler(button_click, pattern='^cancel_action$')
            ],
            ASK_PUBLIC_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_public_price),
                CallbackQueryHandler(admin_cancel_action, pattern='^admin_cancel$')
            ],
            ASK_COMMISSION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_commission),
                CallbackQueryHandler(admin_cancel_action, pattern='^admin_cancel$')
            ],
            ASK_DISCOUNT_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_discount_price),
                CallbackQueryHandler(admin_cancel_action, pattern='^admin_cancel$')
            ],
        },
        fallbacks=[CommandHandler('cancel', cancel), CommandHandler('start', start)],
        allow_reentry=True  # এটি যেকোনো জায়গা থেকে মেনু রিস্টার্ট করতে সাহায্য করবে
    )

    # কমান্ড এবং মাস্টার হ্যান্ডলার অ্যাপে যুক্ত করা
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(master_conv_handler)


    print("Bot is running! Press Ctrl+C to stop.")
    app.run_polling()


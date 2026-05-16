"""
====================================================
  بوت تبادل حسابات eFootball 26
  eFootball Account Trading Bot — Full MVP (Patched)
  -----------------------------------------------
  Patch: Restart-Safe | DB-State | Locking | Auto-Clean
====================================================
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler,
    filters, ContextTypes
)
import psycopg
from psycopg.rows import dict_row

# =====================================================
#  إعدادات — ضع بياناتك هنا
# =====================================================
BOT_TOKEN  = os.environ.get("BOT_TOKEN",  "ضع_التوكن_هنا")
DB_URL     = os.environ.get("DATABASE_URL","ضع_رابط_سوبابيز_هنا")
ADMIN_ID   = int(os.environ.get("ADMIN_ID","ضع_الـID_بتاعك_هنا"))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


# =====================================================
#  States — مراحل المحادثة
# =====================================================
(
    S_NAME, S_PHONE, S_BOOSTERS, S_PLAYERS,
    S_RANK, S_PRICE, S_DESC, S_MEDIA, S_CONFIRM
) = range(9)

(B_CODE, B_CONFIRM, B_PAYMENT) = range(9, 12)


# =====================================================
#  قاعدة البيانات
# =====================================================
def db():
    return psycopg.connect(
        DB_URL,
        row_factory=dict_row
    )


def init_db():
    c = db()
    cur = c.cursor()

    # ── الجداول الأساسية ──────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id            SERIAL PRIMARY KEY,
            telegram_id   BIGINT UNIQUE NOT NULL,
            username      TEXT,
            full_name     TEXT,
            phone         TEXT,
            created_at    TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS listings (
            id              SERIAL PRIMARY KEY,
            code            TEXT UNIQUE,
            seller_id       BIGINT,
            boosters        INTEGER,
            rare_players    TEXT,
            rank_info       TEXT,
            price           INTEGER,
            description     TEXT,
            media_ids       TEXT,
            status          TEXT DEFAULT 'ACTIVE',
            locked_by       BIGINT,
            created_at      TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id              SERIAL PRIMARY KEY,
            code            TEXT UNIQUE,
            listing_id      INTEGER,
            seller_id       BIGINT,
            buyer_id        BIGINT,
            amount          INTEGER,
            status          TEXT DEFAULT 'PAYMENT_PENDING',
            seller_email    TEXT,
            seller_pass     TEXT,
            verify_end      TIMESTAMP,
            created_at      TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS queue (
            id          SERIAL PRIMARY KEY,
            listing_id  INTEGER,
            user_id     BIGINT,
            created_at  TIMESTAMP DEFAULT NOW()
        )
    """)

    # ── PATCH: أعمدة جديدة للـ state المستديم ──────────
    # flow_state: مرحلة الصفقة مخزنة في DB (مش في الذاكرة)
    cur.execute("""
        ALTER TABLE transactions
        ADD COLUMN IF NOT EXISTS flow_state TEXT DEFAULT 'INIT';
    """)
    # state_payload: بيانات إضافية مؤقتة مع كل مرحلة
    cur.execute("""
        ALTER TABLE transactions
        ADD COLUMN IF NOT EXISTS state_payload JSONB DEFAULT '{}'::jsonb;
    """)
    # locked: علامة احتياطية ضد التوازي
    cur.execute("""
        ALTER TABLE transactions
        ADD COLUMN IF NOT EXISTS locked BOOLEAN DEFAULT FALSE;
    """)
    # completed_at: وقت اكتمال الصفقة للمراجعة
    cur.execute("""
        ALTER TABLE transactions
        ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP;
    """)
    # index على status لسرعة الاستعلام تحت الضغط
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_tx_status
        ON transactions(status);
    """)
    # ────────────────────────────────────────────────────

    c.commit()
    cur.close()
    c.close()
    log.info("✅ Database ready")


# =====================================================
#  مساعدات قاعدة البيانات — الأصلية
# =====================================================
def clean(text: str) -> str:
    """شيل أي يوزر أو رقم تليفون من النص"""
    text = re.sub(r'@\w+', '', text)
    text = re.sub(r't\.me/\w+', '', text)
    text = re.sub(r'(\+20|0)?1[0-9]{9}', '***', text)
    for kw in ['واتس', 'وتساب', 'تليجرام', 'telegram', 'whatsapp']:
        text = re.sub(kw, '***', text, flags=re.IGNORECASE)
    return text.strip()


def make_code(prefix: str, n: int) -> str:
    return f"{prefix}-{n:05d}"


def get_or_create_user(tid: int, username=None):
    c = db()
    cur = c.cursor()
    cur.execute("SELECT * FROM users WHERE telegram_id=%s", (tid,))
    u = cur.fetchone()
    if not u:
        cur.execute(
            "INSERT INTO users (telegram_id,username) VALUES (%s,%s) RETURNING *",
            (tid, username)
        )
        u = cur.fetchone()
        c.commit()
    cur.close(); c.close()
    return u


def fetch_one(table: str, **where):
    c = db()
    cur = c.cursor()
    conds = " AND ".join(f"{k}=%s" for k in where)
    cur.execute(f"SELECT * FROM {table} WHERE {conds}", list(where.values()))
    row = cur.fetchone()
    cur.close(); c.close()
    return row


def run_sql(sql: str, params=()):
    c = db()
    cur = c.cursor()
    cur.execute(sql, params)
    c.commit()
    cur.close(); c.close()


# =====================================================
#  PATCH: مساعدات الصفقات الآمنة
# =====================================================

def tx_lock(tx_id: int):
    """
    يفتح connection ويضع قفل FOR UPDATE على الصفقة.
    ⚠️ المُستدعي مسؤول عن: c.commit() ثم cur.close() ثم c.close()
    يضمن: لا يمكن لاثنين تنفيذ نفس الصفقة في نفس الوقت.
    """
    c = db()
    cur = c.cursor()
    cur.execute(
        "SELECT * FROM transactions WHERE id=%s FOR UPDATE",
        (tx_id,)
    )
    row = cur.fetchone()
    return c, cur, row


def tx_update_state(tx_id: int, state: str, payload=None):
    """
    يحدث flow_state و state_payload في DB في عملية واحدة atomic.
    يستخدم run_sql (connection منفصل) — لا يُستخدم داخل tx_lock.
    """
    payload = payload or {}
    run_sql(
        """
        UPDATE transactions
        SET flow_state = %s,
            state_payload = %s
        WHERE id = %s
        """,
        (state, json.dumps(payload), tx_id)
    )


def tx_get(tx_id: int):
    """جلب الصفقة من DB مباشرة."""
    return fetch_one('transactions', id=tx_id)


def tx_clear_sensitive(tx_id: int):
    """
    حذف بيانات الحساب الحساسة من DB بعد اكتمال الصفقة.
    يحمي من: تسريب الإيميل والباسورد لو اتسرب الـ DB.
    """
    run_sql(
        """
        UPDATE transactions
        SET seller_email  = NULL,
            seller_pass   = NULL,
            state_payload = '{}'::jsonb
        WHERE id = %s
        """,
        (tx_id,)
    )


# =====================================================
#  /start  و  /help
# =====================================================
async def cmd_start(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    get_or_create_user(u.effective_user.id, u.effective_user.username)
    await u.message.reply_text(
        "🎮 *أهلاً بك في بوت تبادل حسابات eFootball 26!*\n\n"
        "اختار:\n"
        "/sell — بيع حساب\n"
        "/buy  — شراء حساب\n"
        "/help — مساعدة",
        parse_mode='Markdown'
    )


async def cmd_help(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        "📖 *شرح الاستخدام:*\n\n"
        "*بيع حساب:*\n"
        "اكتب /sell وابعت البيانات خطوة بخطوة\n\n"
        "*شراء حساب:*\n"
        "اكتب /buy وابعت رقم الإعلان\n\n"
        "*مشكلة أو نزاع؟*\n"
        "البوت هيبلغ صاحب الجروب فوراً",
        parse_mode='Markdown'
    )


# =====================================================
#  رحلة البائع
# =====================================================
async def sell_start(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = get_or_create_user(u.effective_user.id, u.effective_user.username)
    if user['phone']:
        await u.message.reply_text("🎮 *بيع حساب*\n\nكام بوستر في الحساب؟", parse_mode='Markdown')
        return S_BOOSTERS
    await u.message.reply_text(
        "👋 *أهلاً — محتاج بياناتك الأول*\n\nاكتب اسمك:",
        parse_mode='Markdown'
    )
    return S_NAME


async def sell_name(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data['name'] = u.message.text.strip()
    await u.message.reply_text("📱 رقم فودافون كاش بتاعك:")
    return S_PHONE


async def sell_phone(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ph = u.message.text.strip()
    if not re.match(r'^01[0-9]{9}$', ph):
        await u.message.reply_text("❌ رقم غلط — ابعت رقم مصري 11 رقم:")
        return S_PHONE
    run_sql(
        "UPDATE users SET full_name=%s, phone=%s WHERE telegram_id=%s",
        (ctx.user_data['name'], ph, u.effective_user.id)
    )
    await u.message.reply_text("✅ تم التسجيل!\n\nكام بوستر في الحساب؟")
    return S_BOOSTERS


async def sell_boosters(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        ctx.user_data['boosters'] = int(u.message.text.strip())
        await u.message.reply_text("⭐ اسامي اللعيبة النوادر؟ (مفصولين بفاصلة)")
        return S_PLAYERS
    except Exception:
        await u.message.reply_text("❌ ابعت رقم بس:")
        return S_BOOSTERS


async def sell_players(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data['players'] = clean(u.message.text)
    await u.message.reply_text("🏆 الرانك والديفيجن؟ (مثال: Division 1 — Rank 200)")
    return S_RANK


async def sell_rank(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data['rank'] = clean(u.message.text)
    await u.message.reply_text("💰 السعر بالجنيه؟ (أرقام بس)")
    return S_PRICE


async def sell_price(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        price = int(u.message.text.strip())
        if price <= 0:
            raise ValueError
        ctx.user_data['price'] = price
        await u.message.reply_text(
            "📝 اكتب وصف للحساب:\n"
            "⚠️ أي يوزر تليجرام أو رقم في الوصف هيتمسح تلقائياً"
        )
        return S_DESC
    except Exception:
        await u.message.reply_text("❌ ابعت رقم صح:")
        return S_PRICE


async def sell_desc(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data['desc'] = clean(u.message.text)
    ctx.user_data['media'] = []
    await u.message.reply_text(
        "📸 ابعت صور الحساب\n"
        "لما تخلص اكتب: *تم*",
        parse_mode='Markdown'
    )
    return S_MEDIA


async def sell_media(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if u.message.photo:
        ctx.user_data['media'].append(u.message.photo[-1].file_id)
        await u.message.reply_text(
            f"✅ صورة {len(ctx.user_data['media'])} اتحفظت — كمّل أو اكتب *تم*",
            parse_mode='Markdown'
        )
        return S_MEDIA
    if u.message.text and u.message.text.strip() == 'تم':
        if not ctx.user_data['media']:
            await u.message.reply_text("❌ لازم صورة واحدة على الأقل:")
            return S_MEDIA
        return await sell_review(u, ctx)
    await u.message.reply_text("ابعت صورة أو اكتب *تم*", parse_mode='Markdown')
    return S_MEDIA


async def sell_review(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    d = ctx.user_data
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ تأكيد النشر", callback_data="sc_yes")],
        [InlineKeyboardButton("❌ إلغاء",        callback_data="sc_no")]
    ])
    await u.message.reply_text(
        f"📋 *مراجعة الإعلان:*\n\n"
        f"🎮 البوسترات: {d['boosters']}\n"
        f"⭐ اللعيبة: {d['players']}\n"
        f"🏆 الرانك: {d['rank']}\n"
        f"💰 السعر: {d['price']} جنيه\n"
        f"📝 الوصف: {d['desc']}\n"
        f"📸 الصور: {len(d['media'])} صورة\n\n"
        "تأكيد؟",
        parse_mode='Markdown',
        reply_markup=kb
    )
    return S_CONFIRM


async def sell_confirm_cb(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    await q.answer()
    if q.data == "sc_no":
        await q.edit_message_text("❌ تم إلغاء الإعلان.")
        return ConversationHandler.END

    d = ctx.user_data
    c = db()
    cur = c.cursor()
    cur.execute(
        """INSERT INTO listings
           (seller_id,boosters,rare_players,rank_info,price,description,media_ids)
           VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (q.from_user.id, d['boosters'], d['players'],
         d['rank'], d['price'], d['desc'], ','.join(d['media']))
    )
    lid = cur.fetchone()['id']
    code = make_code("EF", lid)
    cur.execute("UPDATE listings SET code=%s WHERE id=%s", (code, lid))
    c.commit()
    cur.close(); c.close()

    await q.edit_message_text(
        f"🎉 *تم نشر إعلانك!*\n\n"
        f"📌 رقم الإعلان: `{code}`\n\n"
        f"شارك الرقم ده مع المشترين\n"
        f"هنبلغك فوراً لما يجي مشتري ✅",
        parse_mode='Markdown'
    )
    return ConversationHandler.END


# =====================================================
#  رحلة المشتري
# =====================================================
async def buy_start(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    get_or_create_user(u.effective_user.id, u.effective_user.username)
    await u.message.reply_text(
        "🛒 *شراء حساب*\n\nابعت رقم الإعلان:\n(مثال: EF-00001)",
        parse_mode='Markdown'
    )
    return B_CODE


async def buy_code(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    code = u.message.text.strip().upper()
    lst = fetch_one('listings', code=code)

    if not lst:
        await u.message.reply_text("❌ رقم مش موجود — تأكد وحاول تاني:")
        return B_CODE
    if lst['seller_id'] == u.effective_user.id:
        await u.message.reply_text("❌ مينفعش تشتري إعلانك أنت!")
        return ConversationHandler.END
    if lst['status'] == 'SOLD':
        await u.message.reply_text("❌ الحساب ده اتباع بالفعل.")
        return ConversationHandler.END
    if lst['status'] == 'LOCKED':
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ أيوه، ضيفني للانتظار", callback_data=f"queue_{lst['id']}")
        ]])
        await u.message.reply_text(
            f"⏳ *الحساب {code} محجوز حالياً*\n\n"
            "عايز أضيفك لقائمة الانتظار؟",
            parse_mode='Markdown',
            reply_markup=kb
        )
        return ConversationHandler.END

    ctx.user_data['lst'] = dict(lst)

    # أبعت الصور
    for mid in (lst['media_ids'] or '').split(',')[:4]:
        if mid:
            try:
                await u.message.reply_photo(mid)
            except Exception:
                pass

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ نعم، عايز أشتري", callback_data="by_yes")],
        [InlineKeyboardButton("❌ لا",               callback_data="by_no")]
    ])
    await u.message.reply_text(
        f"📋 *تفاصيل الحساب {code}:*\n\n"
        f"🎮 البوسترات: {lst['boosters']}\n"
        f"⭐ اللعيبة: {lst['rare_players']}\n"
        f"🏆 الرانك: {lst['rank_info']}\n"
        f"📝 الوصف: {lst['description']}\n"
        f"💰 السعر: {lst['price']} جنيه\n\n"
        "عايز تشتري؟",
        parse_mode='Markdown',
        reply_markup=kb
    )
    return B_CONFIRM


async def buy_confirm_cb(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    await q.answer()

    # قائمة انتظار
    if q.data.startswith("queue_"):
        lid = int(q.data.split("_")[1])
        c = db()
        cur = c.cursor()
        cur.execute(
            "SELECT id FROM queue WHERE listing_id=%s AND user_id=%s",
            (lid, q.from_user.id)
        )
        if cur.fetchone():
            await q.edit_message_text("أنت أصلاً في قائمة الانتظار ✅")
        else:
            cur.execute(
                "INSERT INTO queue (listing_id,user_id) VALUES (%s,%s)",
                (lid, q.from_user.id)
            )
            c.commit()
            await q.edit_message_text("✅ تم إضافتك لقائمة الانتظار")
        cur.close(); c.close()
        return ConversationHandler.END

    if q.data == "by_no":
        await q.edit_message_text("تمام — لو غيرت رأيك ابعت /buy")
        return ConversationHandler.END

    lst = ctx.user_data.get('lst')
    if not lst:
        await q.edit_message_text("❌ في مشكلة — ابدأ من أول بـ /buy")
        return ConversationHandler.END

    # PATCH: SKIP LOCKED — حماية من تزاحم المشترين
    # لو مشتري تاني فاتح transaction على نفس الإعلان
    # SKIP LOCKED بترجع None بدل ما تستنى، فنبلغه فوراً
    c = db()
    cur = c.cursor()
    cur.execute(
        "SELECT * FROM listings WHERE id=%s FOR UPDATE SKIP LOCKED",
        (lst['id'],)
    )
    row = cur.fetchone()

    if not row:
        # الإعلان مقفول من connection تانية في نفس اللحظة
        c.rollback()
        cur.close(); c.close()
        await q.edit_message_text(
            "❌ الحساب بيتعالج حالياً — حاول بعد ثواني"
        )
        return ConversationHandler.END

    if row['status'] != 'ACTIVE':
        c.rollback()
        cur.close(); c.close()
        await q.edit_message_text("❌ معلش — الحساب اتحجز من شخص تاني قبلك بثواني!")
        return ConversationHandler.END

    cur.execute(
        "UPDATE listings SET status='LOCKED', locked_by=%s WHERE id=%s",
        (q.from_user.id, lst['id'])
    )
    c.commit()
    cur.close(); c.close()

    await q.edit_message_text(
        f"✅ *تم حجز الحساب ليك!*\n\n"
        f"💰 المبلغ: *{lst['price']} جنيه*\n\n"
        f"📱 ادفع على:\n"
        f"فودافون كاش: *01XXXXXXXXX*\n"
        f"(رقم صاحب الجروب)\n\n"
        f"⚠️ اكتب رقم الإعلان *{lst['code']}* في ملاحظة الدفع\n\n"
        f"بعد الدفع ابعتلي *صورة الإيصال*",
        parse_mode='Markdown'
    )
    return B_PAYMENT


async def buy_receipt(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not u.message.photo:
        await u.message.reply_text("📸 ابعت صورة الإيصال:")
        return B_PAYMENT

    lst = ctx.user_data.get('lst')
    if not lst:
        return ConversationHandler.END

    receipt = u.message.photo[-1].file_id
    c = db()
    cur = c.cursor()
    cur.execute(
        """INSERT INTO transactions
           (listing_id,seller_id,buyer_id,amount,status)
           VALUES (%s,%s,%s,%s,'PAYMENT_REVIEW') RETURNING id""",
        (lst['id'], lst['seller_id'], u.effective_user.id, lst['price'])
    )
    tx_id = cur.fetchone()['id']
    tx_code = make_code("TX", tx_id)
    cur.execute("UPDATE transactions SET code=%s WHERE id=%s", (tx_code, tx_id))
    c.commit()
    cur.close(); c.close()

    buyer = fetch_one('users', telegram_id=u.effective_user.id)

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ تأكيد الدفع",  callback_data=f"adm_ok_{tx_id}"),
        InlineKeyboardButton("❌ رفض",          callback_data=f"adm_no_{tx_id}")
    ]])

    await ctx.bot.send_photo(
        ADMIN_ID, receipt,
        caption=(
            f"💳 *طلب دفع — {tx_code}*\n\n"
            f"الإعلان: {lst['code']}\n"
            f"المبلغ:  {lst['price']} جنيه\n"
            f"المشتري: {buyer['full_name'] or '—'}\n"
            f"تليفون:  {buyer['phone'] or '—'}\n"
            f"يوزر:    @{u.effective_user.username or '—'}"
        ),
        parse_mode='Markdown',
        reply_markup=kb
    )
    await u.message.reply_text(
        f"✅ وصلنا الإيصال!\n\n"
        f"⏳ جاري التحقق — هنبلغك فوراً\n"
        f"رقم الصفقة: `{tx_code}`",
        parse_mode='Markdown'
    )
    return ConversationHandler.END


# =====================================================
#  Admin Callbacks
# =====================================================
async def admin_cb(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    await q.answer()
    if q.from_user.id != ADMIN_ID:
        return

    parts  = q.data.split("_")
    action = parts[1]
    tx_id  = int(parts[2])
    tx     = fetch_one('transactions', id=tx_id)
    if not tx:
        return

    if action == "ok":
        run_sql(
            "UPDATE transactions SET status='TRANSFER_INITIATED' WHERE id=%s",
            (tx_id,)
        )
        await q.edit_message_caption("✅ تم تأكيد الدفع — بدأت عملية النقل")

        # PATCH: حفظ مرحلة "انتظار الإيميل" في DB بدل ctx.bot_data
        # لو البوت وقف هنا، لما يرجع يلاقي flow_state='WAITING_SELLER_EMAIL'
        # في DB ويكمل من نفس المرحلة
        tx_update_state(tx_id, 'WAITING_SELLER_EMAIL')

        await ctx.bot.send_message(
            tx['seller_id'],
            f"🔔 *مشتري دفع للحساب بتاعك!*\n\n"
            f"الصفقة: `{tx['code']}`\n\n"
            f"ابعتلي الإيميل بتاع الحساب:",
            parse_mode='Markdown'
        )
        await ctx.bot.send_message(
            tx['buyer_id'],
            "⏳ تم تأكيد دفعك!\n\nجاري التواصل مع البائع 🔄"
        )

    elif action == "no":
        run_sql(
            "UPDATE transactions SET status='CANCELLED' WHERE id=%s",
            (tx_id,)
        )
        run_sql(
            "UPDATE listings SET status='ACTIVE', locked_by=NULL WHERE id=%s",
            (tx['listing_id'],)
        )
        await q.edit_message_caption("❌ تم رفض الدفع")
        await ctx.bot.send_message(
            tx['buyer_id'],
            "❌ الدفع مش متأكد — تواصل مع صاحب الجروب لو في مشكلة"
        )
        await notify_queue(ctx, tx['listing_id'])


# =====================================================
#  PATCH: Handler عام — يقرأ الـ state من DB
#  لا يعتمد على ctx.bot_data أو الذاكرة
# =====================================================
async def transfer_handler(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = u.effective_user.id
    text = (u.message.text or '').strip()

    # جيب آخر صفقة active بتخص المستخدم ده (بائع أو مشتري)
    c = db()
    cur = c.cursor()
    cur.execute(
        """
        SELECT * FROM transactions
        WHERE (seller_id = %s OR buyer_id = %s)
          AND status NOT IN ('COMPLETED', 'CANCELLED', 'REFUNDED')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (uid, uid)
    )
    tx = cur.fetchone()
    cur.close()
    c.close()

    if not tx:
        # المستخدم مش في صفقة نشطة — تجاهل الرسالة
        return

    # ── البائع يبعت الإيميل ──────────────────────────
    if tx['seller_id'] == uid and tx['flow_state'] == 'WAITING_SELLER_EMAIL':
        # حفظ الإيميل وتحديث المرحلة في نفس العملية
        run_sql(
            """
            UPDATE transactions
            SET seller_email = %s,
                flow_state   = 'WAITING_SELLER_PASS'
            WHERE id = %s
            """,
            (text, tx['id'])
        )
        await u.message.reply_text("✅ الإيميل اتحفظ — دلوقتي ابعتلي الباسورد:")
        return

    # ── البائع يبعت الباسورد ─────────────────────────
    if tx['seller_id'] == uid and tx['flow_state'] == 'WAITING_SELLER_PASS':
        # حفظ الباسورد وتحديث الـ status والمرحلة في نفس العملية
        run_sql(
            """
            UPDATE transactions
            SET seller_pass = %s,
                status      = 'CREDS_SENT',
                flow_state  = 'WAITING_BUYER_CODE'
            WHERE id = %s
            """,
            (text, tx['id'])
        )
        # إعادة جلب الصفقة بعد التحديث للحصول على seller_email المحدث
        tx = fetch_one('transactions', id=tx['id'])

        # ابعت البيانات للمشتري
        await ctx.bot.send_message(
            tx['buyer_id'],
            f"✅ *بيانات الحساب جاهزة!*\n\n"
            f"📧 الإيميل:   `{tx['seller_email']}`\n"
            f"🔑 الباسورد:  `{tx['seller_pass']}`\n\n"
            f"دلوقتي:\n"
            f"١- افتح eFootball / Konami ID\n"
            f"٢- ادخل بالبيانات دي\n"
            f"٣- اطلب تغيير الإيميل لإيميلك الجديد\n"
            f"٤- كونامي هتبعت كود على إيميلك الجديد\n"
            f"٥- ابعت الكود هنا فوراً ✉️",
            parse_mode='Markdown'
        )
        await u.message.reply_text(
            "✅ تم إرسال البيانات للمشتري\n\n"
            "⏳ انتظر — هنبعتلك كود التأكيد لما المشتري يبعهولنا"
        )
        return

    # ── المشتري يبعت الكود ──────────────────────────
    if tx['buyer_id'] == uid and tx['flow_state'] == 'WAITING_BUYER_CODE':
        code = text
        # تحديث المرحلة قبل إرسال الكود (منع التكرار)
        tx_update_state(tx['id'], 'WAITING_CODE_CONFIRM')

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ تم إدخال الكود", callback_data=f"ce_{tx['id']}")
        ]])
        await ctx.bot.send_message(
            tx['seller_id'],
            f"🔔 الكود وصل!\n\n"
            f"الكود: `{code}`\n\n"
            f"ادخله في كونامي اي دي دلوقتي ⚡\n"
            f"وبعد ما تكتبه — اضغط الزرار:",
            parse_mode='Markdown',
            reply_markup=kb
        )
        await u.message.reply_text(
            "✅ الكود اتبعت للبائع\n\nانتظر ثواني — الحساب هيبقى ملكك ✅"
        )
        return


# =====================================================
#  PATCH: الكود اتكتب في كونامي — مع قفل لمنع التكرار
# =====================================================
async def code_entered_cb(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    tx_id = int(q.data.split("_")[1])

    # PATCH: نقفل الصفقة في DB أولاً
    # لو ضغط الزرار مرتين في نفس الوقت، الثانية هتستنى القفل
    # وبعد commit الأولى، الثانية هتلاقي status != 'CREDS_SENT'
    c, cur, tx = tx_lock(tx_id)

    if not tx:
        cur.close()
        c.close()
        await q.answer("الصفقة مش موجودة", show_alert=True)
        return

    if tx['status'] != 'CREDS_SENT':
        # العملية اتنفذت بالفعل من طلب تاني
        cur.close()
        c.close()
        await q.answer("تم تنفيذ العملية بالفعل", show_alert=True)
        return

    # تأكيد الاستلام وتسجيل وقت بداية الفحص
    await q.answer()

    verify_end = datetime.now() + timedelta(hours=1)

    # نفذ التحديث على نفس الـ connection المقفول
    cur.execute(
        "UPDATE transactions SET status='VERIFICATION', verify_end=%s WHERE id=%s",
        (verify_end, tx_id)
    )
    c.commit()
    cur.close()
    c.close()

    await q.edit_message_text("✅ ممتاز — بدأت ساعة الفحص للمشتري")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ تمام — تأكيد الاستلام",   callback_data=f"vok_{tx_id}")],
        [InlineKeyboardButton("⚠️ في مشكلة — فتح نزاع",   callback_data=f"vdp_{tx_id}")]
    ])
    await ctx.bot.send_message(
        tx['buyer_id'],
        "🎉 *تم نقل الحساب!*\n\n"
        "⏱️ عندك *ساعة كاملة* تفحص كل حاجة\n\n"
        "✅ لو تمام → اضغط تأكيد الاستلام\n"
        "⚠️ لو في مشكلة → اضغط فتح نزاع\n\n"
        "⚠️ بعد الساعة مش هينفع تفتح نزاع",
        parse_mode='Markdown',
        reply_markup=kb
    )
    await ctx.bot.send_message(
        tx['seller_id'],
        "⏳ المشتري بيفحص الحساب\n\nهتستلم فلوسك بعد ساعة ✅"
    )

    # جدول مهمتين: تنبيه 10 دقايق قبل الانتهاء + الإغلاق التلقائي
    ctx.application.job_queue.run_once(
        job_50min, when=timedelta(minutes=50),
        data={'tx_id': tx_id}, name=f"j50_{tx_id}"
    )
    ctx.application.job_queue.run_once(
        job_60min, when=timedelta(minutes=60),
        data={'tx_id': tx_id}, name=f"j60_{tx_id}"
    )


# =====================================================
#  Jobs — مهام الـ Timer
# =====================================================
async def job_50min(ctx: ContextTypes.DEFAULT_TYPE):
    """تنبيه للأدمن قبل الانتهاء بـ 10 دقائق"""
    tx_id  = ctx.job.data['tx_id']
    tx     = fetch_one('transactions', id=tx_id)
    if not tx or tx['status'] != 'VERIFICATION':
        return
    seller = fetch_one('users', telegram_id=tx['seller_id'])
    await ctx.bot.send_message(
        ADMIN_ID,
        f"⚠️ *تنبيه — 10 دقائق للانتهاء*\n\n"
        f"الصفقة: {tx['code']}\n"
        f"المبلغ: {tx['amount']} جنيه\n"
        f"البائع: {seller['full_name']}\n"
        f"فودافون كاش: {seller['phone']}\n\n"
        f"استعد لتحويل المبلغ!",
        parse_mode='Markdown'
    )


async def job_60min(ctx: ContextTypes.DEFAULT_TYPE):
    """إغلاق الصفقة تلقائياً بعد انتهاء ساعة الفحص"""
    tx_id  = ctx.job.data['tx_id']
    tx     = fetch_one('transactions', id=tx_id)
    if not tx or tx['status'] != 'VERIFICATION':
        return

    run_sql(
        "UPDATE transactions SET status='COMPLETED', completed_at=NOW() WHERE id=%s",
        (tx_id,)
    )
    run_sql("UPDATE listings SET status='SOLD' WHERE id=%s", (tx['listing_id'],))

    # PATCH: حذف البيانات الحساسة بعد اكتمال الصفقة
    tx_clear_sensitive(tx_id)

    seller = fetch_one('users', telegram_id=tx['seller_id'])
    await ctx.bot.send_message(
        ADMIN_ID,
        f"✅ *انتهت ساعة الفحص — حوّل الفلوس دلوقتي!*\n\n"
        f"الصفقة: {tx['code']}\n"
        f"المبلغ: {tx['amount']} جنيه\n"
        f"فودافون كاش: {seller['phone']}\n"
        f"البائع: {seller['full_name']}",
        parse_mode='Markdown'
    )
    await ctx.bot.send_message(
        tx['buyer_id'],
        "✅ انتهت مدة الفحص — الصفقة مكتملة\nالحساب ملكك 🎮"
    )
    await ctx.bot.send_message(
        tx['seller_id'],
        "✅ تم إتمام الصفقة!\nصاحب الجروب هيحولك فلوسك دلوقتي"
    )


# =====================================================
#  PATCH: التحقق — مع حماية من التكرار وانتهاء المدة
# =====================================================
async def verify_cb(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    parts  = q.data.split("_")
    action = parts[0]   # vok أو vdp
    tx_id  = int(parts[1])
    tx     = fetch_one('transactions', id=tx_id)

    if not tx:
        await q.answer()
        return

    # PATCH: تحقق من انتهاء مدة الفحص
    now = datetime.now()
    if tx['verify_end'] and now > tx['verify_end']:
        await q.answer("انتهت مدة الفحص", show_alert=True)
        return

    # PATCH: منع معالجة الصفقة أكتر من مرة
    if tx['status'] != 'VERIFICATION':
        await q.answer("تم التعامل مع الصفقة بالفعل", show_alert=True)
        return

    await q.answer()

    if action == "vok":
        run_sql(
            "UPDATE transactions SET status='COMPLETED', completed_at=NOW() WHERE id=%s",
            (tx_id,)
        )
        run_sql("UPDATE listings SET status='SOLD' WHERE id=%s", (tx['listing_id'],))

        # PATCH: حذف البيانات الحساسة فوراً عند تأكيد المشتري
        tx_clear_sensitive(tx_id)

        await q.edit_message_text("🎉 تم إتمام الصفقة بنجاح!\nالحساب ملكك دلوقتي 🎮")
        seller = fetch_one('users', telegram_id=tx['seller_id'])
        await ctx.bot.send_message(
            ADMIN_ID,
            f"✅ *تأكيد فوري من المشتري*\n\n"
            f"الصفقة: {tx['code']}\n"
            f"المبلغ: {tx['amount']} جنيه\n"
            f"فودافون كاش: {seller['phone']}\n"
            f"البائع: {seller['full_name']}",
            parse_mode='Markdown'
        )
        await ctx.bot.send_message(
            tx['seller_id'],
            "🎉 المشتري أكد الاستلام!\nصاحب الجروب هيحولك فلوسك ✅"
        )
        # إلغاء مهام الـ Timer اللي بقت بلا لازمة
        for jname in [f"j50_{tx_id}", f"j60_{tx_id}"]:
            jobs = ctx.application.job_queue.get_jobs_by_name(jname)
            for j in jobs:
                j.schedule_removal()

    elif action == "vdp":
        run_sql("UPDATE transactions SET status='DISPUTED' WHERE id=%s", (tx_id,))
        await q.edit_message_text(
            "⚠️ *تم فتح نزاع*\n\n"
            "صاحب الجروب هيتواصل معك فوراً\n"
            "الفلوس محجوزة لحد ما الموضوع يتحل",
            parse_mode='Markdown'
        )
        seller  = fetch_one('users', telegram_id=tx['seller_id'])
        buyer   = fetch_one('users', telegram_id=tx['buyer_id'])
        listing = fetch_one('listings', id=tx['listing_id'])

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("👤 تواصل مع البائع",
                                  url=f"tg://user?id={tx['seller_id']}")],
            [InlineKeyboardButton("👤 تواصل مع المشتري",
                                  url=f"tg://user?id={tx['buyer_id']}")],
            [InlineKeyboardButton("✅ صالح للمشتري (رد الفلوس)",
                                  callback_data=f"dr_buyer_{tx_id}")],
            [InlineKeyboardButton("✅ صالح للبائع (سلم الفلوس)",
                                  callback_data=f"dr_seller_{tx_id}")]
        ])
        await ctx.bot.send_message(
            ADMIN_ID,
            f"🚨 *نزاع جديد — {tx['code']}*\n\n"
            f"البائع: {seller['full_name']} — {seller['phone']} — @{seller['username'] or '—'}\n"
            f"المشتري: {buyer['full_name'] or '—'} — {buyer['phone'] or '—'} — @{buyer['username'] or '—'}\n\n"
            f"الحساب: {listing['code']}\n"
            f"المبلغ: {tx['amount']} جنيه\n"
            f"الوصف: {listing['description']}",
            parse_mode='Markdown',
            reply_markup=kb
        )
        for mid in (listing['media_ids'] or '').split(',')[:4]:
            if mid:
                try:
                    await ctx.bot.send_photo(ADMIN_ID, mid)
                except Exception:
                    pass


# =====================================================
#  حل النزاع من Admin
# =====================================================
async def dispute_cb(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    await q.answer()
    if q.from_user.id != ADMIN_ID:
        return

    parts   = q.data.split("_")
    winner  = parts[1]   # buyer أو seller
    tx_id   = int(parts[2])
    tx      = fetch_one('transactions', id=tx_id)
    if not tx:
        return
    seller  = fetch_one('users', telegram_id=tx['seller_id'])

    if winner == "buyer":
        run_sql("UPDATE transactions SET status='REFUNDED' WHERE id=%s", (tx_id,))
        run_sql(
            "UPDATE listings SET status='ACTIVE', locked_by=NULL WHERE id=%s",
            (tx['listing_id'],)
        )
        # PATCH: حذف البيانات الحساسة بعد إنهاء النزاع
        tx_clear_sensitive(tx_id)

        await ctx.bot.send_message(tx['buyer_id'],
            "✅ تم حل النزاع لصالحك — فلوسك هترجعلك ✅"
        )
        await ctx.bot.send_message(tx['seller_id'],
            "❌ تم حل النزاع لصالح المشتري"
        )
        await q.edit_message_text(
            f"✅ حل لصالح المشتري — ارجع الفلوس ({tx['amount']} جنيه)"
        )
        await notify_queue(ctx, tx['listing_id'])

    elif winner == "seller":
        run_sql("UPDATE transactions SET status='COMPLETED', completed_at=NOW() WHERE id=%s", (tx_id,))
        run_sql("UPDATE listings SET status='SOLD' WHERE id=%s", (tx['listing_id'],))
        # PATCH: حذف البيانات الحساسة بعد إنهاء النزاع
        tx_clear_sensitive(tx_id)

        await ctx.bot.send_message(tx['seller_id'],
            "✅ تم حل النزاع لصالحك — صاحب الجروب هيحولك فلوسك ✅"
        )
        await ctx.bot.send_message(tx['buyer_id'],
            "❌ تم حل النزاع لصالح البائع"
        )
        await q.edit_message_text(
            f"✅ حل لصالح البائع — حوّل {tx['amount']} جنيه لـ {seller['phone']}"
        )


# =====================================================
#  إبلاغ قائمة الانتظار
# =====================================================
async def notify_queue(ctx, listing_id: int):
    c = db()
    cur = c.cursor()
    cur.execute(
        "SELECT * FROM queue WHERE listing_id=%s ORDER BY created_at LIMIT 1",
        (listing_id,)
    )
    nxt = cur.fetchone()
    if nxt:
        cur.execute("DELETE FROM queue WHERE id=%s", (nxt['id'],))
        c.commit()
        lst = fetch_one('listings', id=listing_id)
        await ctx.bot.send_message(
            nxt['user_id'],
            f"🔔 الحساب {lst['code']} بقى متاح تاني!\n"
            f"ابعت رقمه: /buy",
        )
    cur.close(); c.close()


# =====================================================
#  PATCH: استعادة الـ Jobs بعد الـ Restart
# =====================================================
async def restore_jobs(app: Application):
    """
    بعد أي restart، نجيب كل الصفقات اللي في مرحلة VERIFICATION
    ونعيد جدولة مهمة الإغلاق التلقائي ليها.
    يضمن: restart لن يُضيع صفقة في طور الفحص.
    """
    c = db()
    cur = c.cursor()
    cur.execute(
        """
        SELECT * FROM transactions
        WHERE status = 'VERIFICATION'
          AND verify_end IS NOT NULL
        """
    )
    rows = cur.fetchall()
    cur.close()
    c.close()

    now = datetime.now()
    restored = 0

    for tx in rows:
        remaining = tx['verify_end'] - now

        if remaining.total_seconds() <= 0:
            # الوقت انتهى وهو البوت كان واقع — نكمل الصفقة فوراً
            log.warning(
                f"⚠️ صفقة {tx['code']} انتهت وقتها أثناء الـ restart — هتكتمل تلقائياً"
            )
            run_sql(
                "UPDATE transactions SET status='COMPLETED', completed_at=NOW() WHERE id=%s",
                (tx['id'],)
            )
            run_sql(
                "UPDATE listings SET status='SOLD' WHERE id=%s",
                (tx['listing_id'],)
            )
            tx_clear_sensitive(tx['id'])
            continue

        # استعادة مهمة الإغلاق
        app.job_queue.run_once(
            job_60min,
            when=remaining,
            data={'tx_id': tx['id']},
            name=f"j60_{tx['id']}"
        )

        # استعادة مهمة التنبيه لو لسه فيه أكتر من 10 دقايق
        warn_remaining = remaining - timedelta(minutes=10)
        if warn_remaining.total_seconds() > 0:
            app.job_queue.run_once(
                job_50min,
                when=warn_remaining,
                data={'tx_id': tx['id']},
                name=f"j50_{tx['id']}"
            )

        restored += 1
        log.info(f"♻️ استعادة مهمة صفقة {tx['code']} — باقي {remaining}")

    log.info(f"✅ restore_jobs: استُعيد {restored} صفقة")


# =====================================================
#  تشغيل البوت
# =====================================================
def main():
    init_db()

    # PATCH: post_init لاستعادة الـ Jobs بعد كل restart
    async def post_init(app: Application):
        await restore_jobs(app)

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)   # ← يُشغَّل فور بدء البوت
        .build()
    )

    # Conversations
    sell_conv = ConversationHandler(
        entry_points=[CommandHandler("sell", sell_start)],
        states={
            S_NAME:     [MessageHandler(filters.TEXT & ~filters.COMMAND, sell_name)],
            S_PHONE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, sell_phone)],
            S_BOOSTERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, sell_boosters)],
            S_PLAYERS:  [MessageHandler(filters.TEXT & ~filters.COMMAND, sell_players)],
            S_RANK:     [MessageHandler(filters.TEXT & ~filters.COMMAND, sell_rank)],
            S_PRICE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, sell_price)],
            S_DESC:     [MessageHandler(filters.TEXT & ~filters.COMMAND, sell_desc)],
            S_MEDIA:    [
                MessageHandler(filters.PHOTO,                   sell_media),
                MessageHandler(filters.TEXT & ~filters.COMMAND, sell_media),
            ],
            S_CONFIRM:  [CallbackQueryHandler(sell_confirm_cb, pattern=r"^sc_")],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
        allow_reentry=True,
    )

    buy_conv = ConversationHandler(
        entry_points=[CommandHandler("buy", buy_start)],
        states={
            B_CODE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, buy_code)],
            B_CONFIRM: [CallbackQueryHandler(buy_confirm_cb, pattern=r"^by_|^queue_")],
            B_PAYMENT: [MessageHandler(filters.PHOTO, buy_receipt)],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(sell_conv)
    app.add_handler(buy_conv)

    # Callbacks
    app.add_handler(CallbackQueryHandler(admin_cb,        pattern=r"^adm_"))
    app.add_handler(CallbackQueryHandler(code_entered_cb, pattern=r"^ce_"))
    app.add_handler(CallbackQueryHandler(verify_cb,       pattern=r"^vok_|^vdp_"))
    app.add_handler(CallbackQueryHandler(dispute_cb,      pattern=r"^dr_"))

    # Transfer handler — عام (لازم يبقى الأخير)
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        transfer_handler
    ))

    log.info("🚀 Bot is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

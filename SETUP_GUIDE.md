# دليل تشغيل البوت — خطوة بخطوة

## الأدوات المجانية المطلوبة (4 حسابات بس)
1. Telegram — عندك بالفعل
2. GitHub → github.com (مجاني)
3. Supabase → supabase.com (مجاني — قاعدة البيانات)
4. Railway → railway.app (مجاني — الهوستنج)

---

## الخطوة 1 — إنشاء البوت على Telegram

1. افتح تليجرام ودور على: @BotFather
2. ابعت: /newbot
3. اكتب اسم للبوت (مثال: eFootball Trading Bot)
4. اكتب يوزر للبوت (لازم ينتهي بـ bot — مثال: efootball_trade_bot)
5. BotFather هيبعتلك TOKEN — احفظه مهم جداً

---

## الخطوة 2 — اعرف الـ ID بتاعك

1. افتح تليجرام ودور على: @userinfobot
2. ابعت /start
3. هيديك رقمك (مثال: 123456789) — احفظه ده الـ ADMIN_ID

---

## الخطوة 3 — إنشاء قاعدة البيانات على Supabase

1. روح supabase.com واعمل حساب
2. اضغط "New Project"
3. اكتب اسم للمشروع (مثال: efootball-bot)
4. اختار كلمة سر قوية
5. اضغط "Create new project"
6. بعد ما يخلص (دقيقتين) — روح لـ Settings > Database
7. في "Connection string" اختار "URI"
8. انسخ الرابط — هيبدأ بـ postgresql://
   ده الـ DATABASE_URL احفظه

---

## الخطوة 4 — رفع الكود على GitHub

1. روح github.com واعمل حساب
2. اضغط "+" ثم "New repository"
3. اكتب اسم (مثال: efootball-bot)
4. اضغط "Create repository"
5. اضغط "uploading an existing file"
6. ارفع الملفين: bot.py و requirements.txt
7. اضغط "Commit changes"

---

## الخطوة 5 — التشغيل على Railway

1. روح railway.app واعمل حساب بـ GitHub
2. اضغط "New Project"
3. اختار "Deploy from GitHub repo"
4. اختار الـ repo اللي عملته
5. بعد ما يحمل — روح لـ "Variables" وأضف:
   
   BOT_TOKEN    = (التوكن من BotFather)
   DATABASE_URL = (الرابط من Supabase)
   ADMIN_ID     = (رقمك من userinfobot)

6. اضغط "Deploy"
7. روح لـ "Settings" وفي "Start Command" اكتب:
   python bot.py

8. اضغط Deploy تاني

---

## الخطوة 6 — اختبار البوت

1. افتح تليجرام وابعت /start للبوت
2. جرب /sell وابعت بيانات تجريبية
3. من حساب تاني جرب /buy

---

## ملاحظات مهمة

- غيّر رقم فودافون كاش في الكود (ابحث عن 01XXXXXXXXX)
- البوت بيبعت كل طلبات الدفع والنزاعات لحساب الـ ADMIN_ID بتاعك
- لو عايز تضيف channel للإعلانات — بلّغني وأضيف الكود ده

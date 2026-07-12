# Screening Desk — סורק מניות + אפליקציית מובייל

הפרויקט הזה לוקח את הלוגיקה מהסקריפט המקורי שלך (yfinance + defeatbeta-api,
אינדיקטורים טכניים, סינון לפי אסטרטגיה) ופורס אותה כ:

1. **`scanner/`** — אותו סורק, מותאם לריצה אוטומטית בענן במקום Colab
2. **`.github/workflows/scan.yml`** — מריץ את הסורק לפי לוח זמנים, בחינם
3. **`docs/alerts.json`** — התוצאה, מוגשת כ-URL קבוע דרך GitHub Pages
4. **`mobile-app/`** — אפליקציית Expo (React Native) שקוראת מה-URL הזה, ל-iOS ול-Android

## שלב 1 — להעלות לגיטהאב

```bash
cd stock-scanner-app
git init
git add .
git commit -m "initial"
gh repo create screening-desk --public --source=. --push
# או: צור ריפו ב-github.com ואז git remote add origin <url> && git push -u origin main
```

**חשוב:** ערוך את `scanner/tickers.txt` והחלף ברשימה המלאה שלך (7000 הטיקרים).

## שלב 2 — להפעיל GitHub Pages

בריפו: Settings → Pages → Source: **Deploy from a branch** → Branch: `main` / `docs`.
אחרי הריצה הראשונה של הסריקה, הנתונים יהיו זמינים ב:
```
https://<username>.github.io/<repo-name>/alerts.json
```

## שלב 3 — (אופציונלי) טלגרם

Settings → Secrets and variables → Actions → הוסף `TELEGRAM_BOT_TOKEN` ו-`TELEGRAM_CHAT_ID`.
בלעדיהם הסקריפט פשוט מדלג על שליחת ההודעות.

## שלב 4 — להריץ את הסריקה

Actions tab → Stock Scan → Run workflow (ריצה ידנית ראשונה כדי לוודא שהכל עובד),
ואז זה ירוץ אוטומטית לפי הקרון-שדול שבקובץ ה-workflow (כרגע: ימי חול, אחרי סגירת השוק).

⚠️ **שים לב לזמן ריצה:** סריקה של 7000 טיקרים עם קריאות API לנתונים פיננסיים יכולה
לקחת זמן רב ב-GitHub Actions (המכונות פחות חזקות מהסביבה שהייתה ב-Colab).
אם זה לא מסתיים בתוך המכסה (`timeout-minutes: 300`), שקול:
- להקטין את `MAX_WORKERS_SCAN` בקובץ אם אתה נתקל ב-rate limiting
- לפצל לכמה ריצות (לדוגמה 2000 טיקרים בכל ריצה) עם מספר workflow-ים שרצים בזמנים שונים
- לשקול VPS קבוע במקום GitHub Actions אם הסריקה המלאה לא מסתיימת בזמן סביר

## שלב 5 — האפליקציה

```bash
cd mobile-app
npm install
```

ערוך את `config.js` עם ה-URL האמיתי מ-Pages (שלב 2), ואז:

```bash
npx expo start
```

זה יפתח QR code — סרוק עם אפליקציית **Expo Go** בטלפון (iOS/Android) כדי לראות את
האפליקציה רצה על המכשיר האמיתי שלך, בלי לבנות כלום עדיין.

## שלב 6 — פרסום בחנויות

```bash
npm install -g eas-cli
eas login
eas build:configure
eas build --platform ios       # דורש חשבון Apple Developer ($99/שנה)
eas build --platform android   # דורש חשבון Google Play ($25 חד פעמי)
eas submit --platform ios
eas submit --platform android
```

EAS בונה את שתי הגרסאות בענן — אין צורך ב-Mac בשביל iOS. תהליך האישור של אפל
לוקח בד"כ 1-3 ימים, גוגל בד"כ מהיר יותר.

## מבנה

```
stock-scanner-app/
├── .github/workflows/scan.yml   ← מריץ את הסריקה אוטומטית
├── scanner/
│   ├── scan_headless.py         ← הלוגיקה (זהה למקור, בלי Colab)
│   ├── requirements.txt
│   └── tickers.txt              ← ⚠️ ערוך את זה
├── docs/
│   └── alerts.json              ← נוצר אוטומטית, מוגש דרך GitHub Pages
└── mobile-app/
    ├── config.js                ← ⚠️ ערוך את זה (ה-URL)
    ├── App.js                   ← כל האפליקציה
    └── ...
```

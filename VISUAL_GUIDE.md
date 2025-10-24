# 📱 Visual Step-by-Step Guide

## What You'll See When You Start the App

### Step 1: Double-click START_APP.bat (Windows) or START_APP.sh (Mac)

You'll see a black/white window appear with text like this:

```
========================================
 Companies House Financial Extractor
========================================

Installing required packages...
[... some technical stuff ...]

Starting the web app...

Open your web browser and go to:

    http://localhost:5000

Press Ctrl+C to stop the server
========================================
```

**👉 Don't close this window! Keep it open while using the app.**

---

### Step 2: Open Your Web Browser

Open any web browser:
- 🌐 Google Chrome
- 🦊 Firefox
- 🧭 Safari
- 🌊 Microsoft Edge

Type in the address bar:
```
http://localhost:5000
```

Press Enter.

---

### Step 3: You'll See This Beautiful Purple Page

```
┌─────────────────────────────────────────────────┐
│  💼 Companies House Financial Extractor         │
│  Extract financial data from UK company         │
│  accounts - no technical skills needed!         │
└─────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────┐
│                                                  │
│  Company Number                                  │
│  ┌─────────────────────────────────────────┐   │
│  │ e.g., 00000006                           │   │
│  └─────────────────────────────────────────┘   │
│  Enter an 8-digit company number                │
│                                                  │
│  What do you want to see?                       │
│  ┌─────────────────────────────────────────┐   │
│  │ Everything                        ▼     │   │
│  └─────────────────────────────────────────┘   │
│                                                  │
│  ┌───────────────────────────────────────────┐ │
│  │   📊 Extract Financial Data              │ │
│  └───────────────────────────────────────────┘ │
└─────────────────────────────────────────────────┘
```

---

### Step 4: Enter a Company Number

1. Click in the "Company Number" box
2. Type: **00000006** (for testing)
3. Click the purple button "📊 Extract Financial Data"

---

### Step 5: Wait a Few Seconds

You'll see a spinning circle that says:
```
⏳ Fetching data from Companies House...
```

---

### Step 6: See Your Results!

The page will show:

```
┌─────────────────────────────────────────────────┐
│  📈 Financial Results                            │
├─────────────────────────────────────────────────┤
│                                                  │
│  EXAMPLE COMPANY LIMITED                         │
│  Company Number: 00000006                        │
│  Period End: 31/12/2023                          │
│                                                  │
├─────────────────────────────────────────────────┤
│                                                  │
│  ┌──────────────────────────────────────────┐  │
│  │ Item      │ Current │ Previous │ Change  │  │
│  ├──────────────────────────────────────────┤  │
│  │ Cash      │ £100k   │ £90k     │ +11%   │  │
│  │ Assets    │ £250k   │ £230k    │ +8%    │  │
│  │ ...       │ ...     │ ...      │ ...    │  │
│  └──────────────────────────────────────────┘  │
│                                                  │
│  Summary                                         │
│  Total Items: 15                                 │
│  Current Year: £500,000                          │
│                                                  │
│  ┌──────────────┐  ┌─────────────────┐         │
│  │ 💾 Download  │  │ 🔄 New Search   │         │
│  │    CSV       │  │                 │         │
│  └──────────────┘  └─────────────────┘         │
└─────────────────────────────────────────────────┘
```

---

## What Each Button Does

### 📊 Extract Financial Data
- Gets the financial information from Companies House
- Shows it in a nice table
- Takes 5-10 seconds

### 💾 Download CSV
- Saves the data to your computer
- You can open it in Excel or Google Sheets
- File is called "financial_data.csv"

### 🔄 New Search
- Clears everything
- Lets you search for a different company

---

## Where to Find Company Numbers

1. Go to: https://find-and-update.company-information.service.gov.uk
2. Search for any company name
3. The company number is shown at the top (8 digits)
4. Copy it and paste into our app!

**Examples:**
- Tesco: 00445790
- Google UK: 03977902
- Small company: 09252748

---

## Common Questions

### "What's localhost?"
It means "this computer". The app runs on YOUR computer, not the internet.

### "Is my data safe?"
Yes! Everything runs on your computer. Nothing is uploaded or shared.

### "Do I need internet?"
Yes, but only to download data from Companies House. The app itself runs on your computer.

### "Can I close the black window?"
Not while using the app! Keep it open. Close it when you're done.

### "How do I stop the app?"
1. Click on the black window
2. Press Ctrl+C on your keyboard
3. Close the window

---

## You're All Set! 🎉

Just remember:
1. ✅ Double-click START_APP file
2. ✅ Open browser to http://localhost:5000
3. ✅ Type company number
4. ✅ Click button
5. ✅ See results!

**No coding. No terminals. Just clicking! 🖱️**

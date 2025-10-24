# 🎉 How to Use the App (Super Simple!)

No coding needed! Just follow these easy steps:

## Step 1: Install Python (if you don't have it)

### On Windows:
1. Go to https://www.python.org/downloads/
2. Download Python (click the big yellow button)
3. Run the installer
4. ✅ **IMPORTANT:** Check the box that says "Add Python to PATH"
5. Click "Install Now"

### On Mac:
1. Python is usually already installed!
2. If not, go to https://www.python.org/downloads/
3. Download and install

## Step 2: Start the App

### On Windows:
1. Double-click the file called **`START_APP.bat`**
2. Wait a few seconds while it sets up (first time only)
3. Your web browser will show some messages

### On Mac/Linux:
1. Double-click the file called **`START_APP.sh`**
2. Wait a few seconds while it sets up (first time only)
3. Your web browser will show some messages

## Step 3: Open the App in Your Browser

1. Open your web browser (Chrome, Firefox, Safari, etc.)
2. Go to: **http://localhost:5000**
3. You should see a purple page with the app!

## Step 4: Extract Financial Data

1. **Enter a company number** (8 digits)
   - Try **00000006** for testing
   - Or find any UK company number at https://find-and-update.company-information.service.gov.uk

2. **Choose what you want to see:**
   - Everything (all financial data)
   - Balance Sheet only
   - Profit & Loss only

3. **Click "Extract Financial Data"**
   - Wait a few seconds
   - See the results in a nice table!

4. **Download the data** (optional)
   - Click "Download CSV" to save the data
   - Open it in Excel or Google Sheets

## Example Company Numbers to Try

- **00000006** - One of the oldest UK companies
- **09252748** - A typical small company
- **00445790** - Marks & Spencer (big company)

## Tips

- ✅ Company numbers are always 8 digits (like 00000006)
- ✅ The app works in any web browser
- ✅ Your data is private - nothing is saved or shared
- ✅ You can extract data from multiple companies

## Troubleshooting

### "Can't connect to the app"
- Make sure you ran the START_APP file first
- Check that you typed **http://localhost:5000** correctly
- Try refreshing your browser

### "No PDF accounts found"
- Some companies only file electronic (XBRL) accounts, not PDFs
- Try a different company number

### "API key error"
- Your API key is already set up! (41e3bec9-855e-4a37-bb54-35af2cc6bd81)
- If it still doesn't work, the key might be invalid

## Stopping the App

When you're done:
1. Go back to the black window that opened
2. Press **Ctrl+C** on your keyboard
3. Close the window

## Need Help?

- The app works completely in your web browser
- No coding or terminal commands needed
- Just click buttons and type company numbers!

---

**That's it! Enjoy extracting financial data! 📊**

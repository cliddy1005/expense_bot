# Expense Bot

macOS/Safari automation for adding Expensify expenses from a CSV using an existing logged-in Safari session.

## What it does

For each CSV row the bot:

1. Opens the target Expensify report.
2. Clicks **Add Expenses → New Expense → Expense**.
3. Fills Merchant, Date, Total, Category and Description.
4. Attaches the receipt using the green receipt **+ → Import From Computer** flow.
5. Clicks **Save** and waits for the expense form to close.
6. Records a local state key only after the Save is confirmed, so reruns skip confirmed expenses.
7. At the end, changes any **Uncategorized** expenses it can safely identify to **Subsistence**.
8. Checks **Subsistence** expenses for missing receipts, matches them back to the CSV by date + merchant + amount, and re-uploads the matching receipt.
9. **Per Diem expenses are always skipped during the missing-receipt repair pass.**

The bot never submits the Expensify report.

## Requirements

- macOS
- Safari
- Python 3 (the tested path is `/opt/homebrew/bin/python3`)
- An existing logged-in Expensify Safari session

No Selenium, ChromeDriver or Playwright is required.

## Required Safari settings

In Safari:

1. Open **Safari → Settings → Advanced**.
2. Enable **Show features for web developers** / the **Develop** menu.
3. From the menu bar open **Develop**.
4. Enable **Allow JavaScript from Apple Events**.

## Required macOS permissions

Open **System Settings → Privacy & Security**.

Under **Accessibility**, allow your terminal application (for example Terminal or iTerm).

Under **Automation**, allow your terminal application to control:

- Safari
- System Events

These permissions are needed for Safari AppleScript control and the native macOS file picker used for receipts.

## CSV format

Required columns:

```csv
report_url,date,merchant,amount,currency,category,description,receipt_path
```

Example:

```csv
report_url,date,merchant,amount,currency,category,description,receipt_path
"https://www.expensify.com/report?param={%22pageReportID%22:621446405922293,%22keepCollection%22:true}",2026-08-14,Tesco,14.00,GBP,Subsistence,Tesco,/Users/ciaranliddy/Expenses/IMG_1631.PNG
"https://www.expensify.com/report?param={%22pageReportID%22:621446405922293,%22keepCollection%22:true}",2026-08-01,TFL,3.00,GBP,Travel,TFL Travel,/Users/ciaranliddy/Expenses/IMG_1632.PNG
```

The category must exactly match an available Expensify category.

## Run

```bash
/opt/homebrew/bin/python3 ~/expense_bot.py \
  --csv ~/Expenses/expenses.csv
```

Test only the first pending expense:

```bash
/opt/homebrew/bin/python3 ~/expense_bot.py \
  --csv ~/Expenses/expenses.csv \
  --test-first-row
```

Force every CSV row to run again, ignoring local duplicate protection:

```bash
/opt/homebrew/bin/python3 ~/expense_bot.py \
  --csv ~/Expenses/expenses.csv \
  --force
```

**Warning:** `--force` can create duplicate expenses.

Skip the final Uncategorized → Subsistence pass:

```bash
--skip-category-cleanup
```

Skip the final missing Subsistence receipt repair pass:

```bash
--skip-receipt-repair
```

## Duplicate protection

The bot stores confirmed expenses in:

```text
.expensify_safari_existing_state.json
```

The state key includes:

- report URL
- date
- merchant
- amount
- currency
- category
- description
- receipt path

A row is only added to state after Expensify Save has been confirmed by the expense form closing.

## Receipt repair

The final receipt repair pass is intentionally conservative:

- It only uses CSV rows whose category is exactly `Subsistence`.
- It never chooses CSV rows whose merchant/category is `Per Diem`.
- Existing open expenses whose merchant is `Per Diem` are skipped.
- A missing receipt is only uploaded when the open expense uniquely matches one CSV row by date + merchant + amount.
- If a unique match cannot be made, the bot skips/stops rather than uploading the wrong receipt.

## Safety

- Keep Safari open and logged into Expensify.
- Do not click around in Safari while the bot is controlling the expense form.
- Review the completed report before submitting it.
- The bot intentionally never clicks **Submit**.

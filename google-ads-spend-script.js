/**
 * Google Ads Script — writes daily ad spend to a Google Sheet.
 *
 * The MCP server's `daily_profit_report` tool reads this sheet (published as CSV)
 * via the GOOGLE_ADS_SPEND_CSV_URL environment variable.
 *
 * Setup:
 *   1. Create an empty Google Sheet and copy its URL into SPREADSHEET_URL below.
 *   2. Google Ads → Tools → Bulk actions → Scripts → "+" → paste this script.
 *   3. Click Authorize, then Preview/Run once (it backfills the last 30 days).
 *   4. Set Frequency to "Daily" (e.g. 03:00–04:00).
 *   5. In the Sheet: File → Share → Publish to web → choose the tab → CSV → Publish.
 *      Use that link as GOOGLE_ADS_SPEND_CSV_URL.
 */

var SPREADSHEET_URL = 'https://docs.google.com/spreadsheets/d/XXXXXXXX/edit';
var SHEET_NAME = 'spend';
var BACKFILL_DAYS = 30;

function main() {
  var ss = SpreadsheetApp.openByUrl(SPREADSHEET_URL);
  var sheet = ss.getSheetByName(SHEET_NAME) || ss.insertSheet(SHEET_NAME);
  var tz = AdsApp.currentAccount().getTimeZone();

  var end = new Date();
  end.setDate(end.getDate() - 1);
  var start = new Date();
  start.setDate(start.getDate() - BACKFILL_DAYS);

  var query =
    'SELECT segments.date, metrics.cost_micros, metrics.conversions_value ' +
    'FROM customer ' +
    'WHERE segments.date BETWEEN "' + Utilities.formatDate(start, tz, 'yyyy-MM-dd') +
    '" AND "' + Utilities.formatDate(end, tz, 'yyyy-MM-dd') + '"';

  var rows = [];
  var it = AdsApp.search(query);
  while (it.hasNext()) {
    var r = it.next();
    rows.push([
      r.segments.date,
      (Number(r.metrics.costMicros) / 1e6).toFixed(2),
      Number(r.metrics.conversionsValue || 0).toFixed(2),
    ]);
  }
  rows.sort(function (a, b) { return a[0] < b[0] ? -1 : 1; });

  // Rewrite the sheet each run so late cost adjustments are picked up.
  sheet.clearContents();
  sheet.getRange(1, 1, 1, 3).setValues([['date', 'cost', 'conversion_value']]);
  if (rows.length) {
    sheet.getRange(2, 1, rows.length, 3).setNumberFormat('@').setValues(rows);
  }
}

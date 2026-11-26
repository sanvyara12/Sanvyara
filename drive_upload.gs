// Google Apps Script - saves uploaded files into YOUR Drive folder.
// 1) script.google.com -> New project -> paste this -> set FOLDER_ID and KEY below
// 2) Deploy -> New deployment -> Web app -> Execute as: Me, Who has access: Anyone
// 3) ALSO set SHEET_ID below: this same script is your DATABASE (no Google Cloud / service account needed)
// 4) Copy the Web app URL into DRIVE_UPLOAD_URL and KEY into DRIVE_UPLOAD_KEY (server settings)
var FOLDER_ID = 'PASTE_DRIVE_FOLDER_ID';   // default folder; Admin can choose another one inside the app
var SHEET_ID = '1XWda3Vx_B8v-NHW3r6fy_B5tL1XVnOYZEef3s9LV1TQ';  // the part after /spreadsheets/d/ in your Sheet URL
var KEY = 'PASTE_A_LONG_RANDOM_SECRET';

// Open the /exec URL in a browser: it must show {"ok":true}. A Google sign-in page means access is not "Anyone".
function doGet() { return out({ ok: true, hint: 'Sanvyara script is running' }); }

// Run this ONCE from the editor (select authorize -> Run -> Allow) so Drive + Sheet permissions are granted.
function authorize() {
  DriveApp.getFolderById(FOLDER_ID);
  SpreadsheetApp.openById(SHEET_ID).getName();
  MailApp.getRemainingDailyQuota();   // permission to send password reset emails
  Logger.log('All permissions OK');
}

function doPost(e) {
  var lock = LockService.getScriptLock();
  try {
    var d = JSON.parse(e.postData.contents);
    if (d.key !== KEY) return out({ error: 'bad key' });
    if (d.action === 'rows') return out(db(d));          // reading needs no lock (faster)
    if (d.action === 'all') {                            // every table in one request (faster start)
      var all = {};
      for (var t in d.tabs) all[t] = db({ action: 'rows', tab: t, cols: d.tabs[t] }).rows;
      return out({ tabs: all });
    }
    if (d.action === 'mail') {                           // password reset codes
      MailApp.sendEmail({ to: d.to, subject: d.subject, body: d.body, name: 'Sanvyara AI' });
      return out({ sent: true });
    }
    if (d.action === 'folder') return out({ name: DriveApp.getFolderById(d.id).getName() });
    if (d.action === 'read') {            // give a Drive file's content back to the server (so the AI can learn from it)
      var f = DriveApp.getFileById(d.id), mime = f.getMimeType(), blob = f.getBlob(), name = f.getName();
      if (mime.indexOf('application/vnd.google-apps') === 0) { blob = f.getAs('application/pdf'); name += '.pdf'; }
      if (blob.getBytes().length > 20 * 1024 * 1024) return out({ error: 'file too large (max 20 MB)' });
      return out({ name: name, data: Utilities.base64Encode(blob.getBytes()) });
    }
    if (['init', 'append', 'delete'].indexOf(d.action) >= 0) {
      lock.waitLock(25000);                              // only Sheet writes wait for each other
      var res = db(d);
      SpreadsheetApp.flush();
      return out(res);
    }
    var folderId = d.folderId || FOLDER_ID;
    var blob = Utilities.newBlob(Utilities.base64Decode(d.data), d.mime, d.name);
    var file = DriveApp.getFolderById(folderId).createFile(blob);
    file.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);
    return out({ url: file.getUrl(), folderId: folderId });
  } catch (err) {
    return out({ error: String(err) });
  } finally {
    try { lock.releaseLock(); } catch (x) {}
  }
}

// ---------- Google Sheet as database ----------
function sheetOf(name, cols) {
  var b = SpreadsheetApp.openById(SHEET_ID), sh = b.getSheetByName(name) || b.insertSheet(name);
  if (sh.getLastRow() === 0) sh.getRange(1, 1, 1, cols.length).setValues([cols]);
  return sh;
}
function db(d) {
  if (d.action === 'init') {
    for (var t in d.tabs) sheetOf(t, d.tabs[t]);
    return { ok: true };
  }
  var sh = sheetOf(d.tab, d.cols), lr = sh.getLastRow(), lc = sh.getLastColumn();
  var vals = lr ? sh.getRange(1, 1, lr, lc).getDisplayValues() : [d.cols], head = vals[0];
  if (d.action === 'rows') {
    return { rows: vals.slice(1).map(function (r) { var o = {}; head.forEach(function (h, i) { o[h] = r[i]; }); return o; }) };
  }
  if (d.action === 'append') {
    var out2 = d.rows.map(function (r) { return head.map(function (h) { return r[h] == null ? '' : String(r[h]); }); });
    var rg = sh.getRange(lr + 1, 1, out2.length, head.length);
    rg.setNumberFormat('@');            // keep everything as text (never a formula or number)
    rg.setValues(out2);
    return { ok: true };
  }
  if (d.action === 'delete') {
    var ci = head.indexOf(d.col), hit = [];
    for (var i = 1; i < vals.length; i++) if (String(vals[i][ci]) === String(d.val)) hit.push(i + 1);
    for (var k = hit.length - 1; k >= 0; ) {            // delete contiguous runs, bottom first
      var end = k; while (end > 0 && hit[end - 1] === hit[end] - 1) end--;
      sh.deleteRows(hit[end], k - end + 1); k = end - 1;
    }
    return { deleted: hit.length };
  }
}
function out(o) {
  return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON);
}

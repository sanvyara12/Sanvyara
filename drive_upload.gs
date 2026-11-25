// Google Apps Script - saves uploaded files into YOUR Drive folder.
// 1) script.google.com -> New project -> paste this -> set FOLDER_ID and KEY below
// 2) Deploy -> New deployment -> Web app -> Execute as: Me, Who has access: Anyone
// 3) Copy the Web app URL into DRIVE_UPLOAD_URL and KEY into DRIVE_UPLOAD_KEY (server settings)
var FOLDER_ID = 'PASTE_DRIVE_FOLDER_ID';   // the part after /folders/ in the folder URL
var KEY = 'PASTE_A_LONG_RANDOM_SECRET';

function doPost(e) {
  try {
    var d = JSON.parse(e.postData.contents);
    if (d.key !== KEY) return out({ error: 'bad key' });
    if (d.action === 'read') {            // give a Drive file's content back to the server (so the AI can learn from it)
      var f = DriveApp.getFileById(d.id), mime = f.getMimeType(), blob = f.getBlob(), name = f.getName();
      if (mime.indexOf('application/vnd.google-apps') === 0) { blob = f.getAs('application/pdf'); name += '.pdf'; }
      if (blob.getBytes().length > 9000000) return out({ error: 'file too large (max ~9 MB)' });
      return out({ name: name, data: Utilities.base64Encode(blob.getBytes()) });
    }
    var blob = Utilities.newBlob(Utilities.base64Decode(d.data), d.mime, d.name);
    var file = DriveApp.getFolderById(FOLDER_ID).createFile(blob);
    file.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);
    return out({ url: file.getUrl() });
  } catch (err) {
    return out({ error: String(err) });
  }
}
function out(o) {
  return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON);
}

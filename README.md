# R-Shift → iPhoneカレンダー自動同期

R-ShiftへPythonでログインし、取得した勤務シフトをiCalendar（`.ics`）へ変換する仕組みです。GitHub Actionsで定期実行し、GitHub PagesでICSを配信すれば、iPhoneの「照会カレンダー」として利用できます。

## ⚠️ セキュリティ

現在のリポジトリがPublicの場合、`docs/shift_calendar.ics` をGitHub Pagesで公開すると勤務予定も公開されます。**勤務情報を公開したくない場合はPrivateリポジトリにしてください。** R-ShiftのID・パスワードはコードに書かず、GitHub Actions Secretsへ登録します。

## 1. GitHub Secrets

Repository → **Settings → Secrets and variables → Actions → New repository secret** から以下を登録します。

- `RSHIFT_USER_ID`：R-ShiftログインID
- `RSHIFT_PASSWORD`：R-Shiftパスワード

## 2. GitHub Actions Variables

必要に応じて以下を **Settings → Secrets and variables → Actions → Variables** に登録できます。未設定時は既定値を使用します。

- `RSHIFT_BASE_URL` = `https://sgy5zm.rshift.jp`
- `RSHIFT_LOGIN_URL` = `https://sgy5zm.rshift.jp/staff/login/`
- `RSHIFT_STAFF_PAGE_URL` = `https://sgy5zm.rshift.jp/staffpage/`
- `RSHIFT_ID_FIELD` = `login_id`
- `RSHIFT_PASSWORD_FIELD` = `password`

実際のR-Shift画面でフォーム名やURLが異なる場合は変更してください。

## 3. 初回テスト

Actions → **R-Shift sync** → **Run workflow** を実行します。成功すると `docs/shift_calendar.ics` が生成されます。

シフトを0件取得した場合は既存ICSを上書きしない安全設計です。HTML構造変更時に空カレンダーで既存データを消す事故を防ぎます。

## 4. GitHub Pages

Repository → Settings → Pages → Build and deployment で、Sourceを **Deploy from a branch**、Branchを `main` / `/docs` に設定します。

公開URLは通常、次の形式です。

`https://sawa6101-code.github.io/r-shift/shift_calendar.ics`

## 5. iPhoneへ登録

iPhoneで「設定」→「カレンダー」→「アカウント」→「アカウントを追加」→「その他」→「照会カレンダーを追加」を開き、ICS URLを登録します。

以後、GitHub ActionsがICSを更新すると、iPhoneの照会カレンダーもApple側のスケジュールに従って再取得されます。GitHub Actionsの更新間隔とiPhoneの反映間隔は一致しません。

## 6. 自動実行

`.github/workflows/sync.yml` は**2時間ごと**に実行する設定です。Actions画面から手動実行も可能です。

## 7. ローカル実行

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export RSHIFT_USER_ID='YOUR_ID'
export RSHIFT_PASSWORD='YOUR_PASSWORD'
python rshift_sync.py
```

Windows PowerShell：

```powershell
$env:RSHIFT_USER_ID='YOUR_ID'
$env:RSHIFT_PASSWORD='YOUR_PASSWORD'
python rshift_sync.py
```

## 8. HTML解析について

`rshift_sync.py` は `tr.shift-row`、または一般的な `table tr` から日付・開始時刻・終了時刻を抽出する汎用パーサーを搭載しています。ただし、実際のR-ShiftのHTML構造、CSRF、JavaScript/API方式などによって調整が必要です。

特に確認が必要なのは以下です。

- ログインフォームのaction
- ID/パスワードのinput name
- CSRF等のhidden input
- ログイン後のシフト一覧URL
- 日付・勤務開始・勤務終了のDOM
- JavaScript/API経由でシフトを取得しているか

実サイトのHTMLを確認できれば、セレクタを固定して取得精度を上げられます。

## ファイル構成

```text
r-shift/
├─ rshift_sync.py
├─ requirements.txt
├─ .gitignore
├─ README.md
├─ docs/
│  └─ shift_calendar.ics  # Actions実行後に生成
└─ .github/
   └─ workflows/
      └─ sync.yml
```

## 利用上の注意

R-Shiftの利用規約、所属先の情報セキュリティ規程、勤務情報の外部公開に関する規程を確認してください。サイト側で自動アクセス・スクレイピングが禁止されている場合は使用しないでください。

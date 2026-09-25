# Installing your newsroom on AWS (beginner guide)

Time: about 45 minutes the first time. Cost: roughly $12–24 a month for the server, plus AI use. Check current prices on the Lightsail pricing page.

You'll need:
- An AWS account (aws.amazon.com), with a payment card.
- A domain name, such as `rivertondaily.com`. You can buy one from any registrar (Namecheap, Cloudflare, Porkbun or AWS Route 53).
- A Claude API key from console.anthropic.com. You can add it later.
- This project as a `.zip` file, and a free GitHub account (step 5).

---

## 1. Protect your AWS account (5 min)

1. Sign in to AWS. Click your name (top right) → **Security credentials** → **Assign MFA device**, and set up two-step login with your phone.
2. Search for **Budgets** → **Create budget** → **Use a template** → **Monthly cost budget**. Set it to about $40. AWS will then email you if spending goes over that amount.

## 2. Create the server (10 min)

1. Search for **Lightsail** and open it.
2. Click **Create instance**.
3. Choose **Linux/Unix** → **OS Only** → **Ubuntu 24.04 LTS**.
4. Choose a plan with **at least 2 GB of memory**.
5. Name it `newsroom` and click **Create instance**.
6. Wait until it says **Running**.

## 3. Give it a permanent address and open HTTPS (5 min)

1. Open the instance → **Networking** tab.
2. Under **IPv4 networking**, click **Attach static IP**. Create one and attach it. Write down the number, such as `3.91.20.14`.
3. On the same tab, under **IPv4 Firewall**, click **Add rule**. Choose **HTTPS** and click **Create**. There should now be rules for SSH, HTTP and HTTPS.

## 4. Point your domain at the server (5 min, then a short wait)

At your domain registrar, open the DNS settings for your domain and add:

| Type | Name / Host | Value |
|---|---|---|
| A | `@` | your static IP |

If you want the site on a subdomain such as `news.yourtown.com`, use `news` as the name instead of `@`.

DNS changes usually take a few minutes, sometimes up to an hour.

## 5. Put the code on GitHub (10 min, once)

GitHub keeps your newsroom's code safe, lets the server download it, and makes updates easy. It also lets any future Claude session pick up exactly where the last one left off.

1. Create a free account at github.com.
2. Click **+** (top right) → **New repository**. Name it `newsroom`.
   - Choose **Public**. The code holds no passwords or keys: those live only on your server.
   - If you'd rather keep it **Private**, see "Private repository" at the end of this guide.
3. On the new repository page, click **uploading an existing file**. Unzip `newsroom.zip` on your computer, then drag everything inside the `newsroom` folder into the page, including the `app` and `deploy` folders. Click **Commit changes**.

## 6. Install the newsroom (10 min)

1. In Lightsail, open the instance and click **Connect using SSH**. A terminal opens in your browser.
2. Copy the command below into a text editor first. Replace the three parts in CAPITALS:
   - `YOURNAME`: your GitHub username.
   - `yourdomain.com`: your domain, with no `https://`.
   - `YOUR-SETUP-PHRASE`: make up a phrase with no spaces, such as `blue-heron-4417`, and write it down. You'll type it once, in the setup wizard. It stops anyone else from claiming your site before you do.

   ```
   curl -fsSL https://raw.githubusercontent.com/YOURNAME/newsroom/main/deploy/install.sh | sudo DOMAIN=yourdomain.com SETUP_CODE=YOUR-SETUP-PHRASE REPO_URL=https://github.com/YOURNAME/newsroom.git bash
   ```

3. Paste it into the browser terminal (right-click → Paste) and press Enter.
4. Wait 5–10 minutes. It's finished when you see `== Done`.

## 7. Set up your newsroom (5 min)

Open `https://your-domain` in your browser. The setup wizard appears.

1. Type your setup code, then create your owner account.
2. Enter your publication name, town, county, state and time zone.
3. Paste your Claude API key and set a monthly spending cap.
4. Tick the sources your town has, and paste each one's address.

That's it. Sources are checked within a few minutes, and new items appear on each source's approval page for you to pick from. The weather page is set up for your town automatically; adjust it under **Weather** in the dashboard.

## 8. Turn on backups (2 min)

The newsroom makes its own backup every night at 3 a.m. (Settings → Backups). As a second safety net:

1. In Lightsail, open the instance → **Snapshots**.
2. Turn on **Automatic snapshots**.

A snapshot is a copy of the whole server that you can restore with one click.

---

## Day-to-day

- **Your newsroom:** `https://your-domain/admin`.
- **Install the app on your phone:** open your site on the phone, then choose "Add to Home Screen" (Share menu on iPhone; Install prompt on Android). Tap **Newsroom** in the app to approve stories.
- **Email notifications:** Settings → System (outgoing email) and Settings → Notifications. Amazon SES works well and is cheap. Any SMTP email service also works.
- **Locked out?** In the Lightsail browser terminal:
  ```
  cd /opt/newsroom && sudo docker compose exec web python -m app reset-password you@example.com
  ```

## Updating to a new version

1. Unzip the new `newsroom.zip` on your computer.
2. On your GitHub repository page, click **Add file → Upload files**. Drag everything inside the `newsroom` folder into the page, including the `app`, `deploy` and `tests` folders, then click **Commit changes**. Files with the same name are replaced.
3. In the Lightsail browser terminal, run:
```
sudo bash /opt/newsroom/deploy/update.sh
```
It makes a backup first, then rebuilds and restarts. Your stories, sources, settings and logins carry over. If anything goes wrong, restore last night's snapshot in Lightsail.

## If something goes wrong

| What you see | What to do |
|---|---|
| The site doesn't load at all | Check step 3: the HTTPS firewall rule and the static IP. Check step 4: DNS may still be updating. |
| A "certificate" or "not secure" error | DNS wasn't pointing at the server yet when it started. Wait 10 minutes, then run `cd /opt/newsroom && sudo docker compose restart caddy`. |
| The install stopped with an error | Read the log: `sudo tail -50 /var/log/newsroom-install.log`. Paste it to Claude for help. |
| A source shows "Problem" | Open it. The message says what's wrong. Use **Test this source** after fixing. |
| Develop or Write does nothing useful | Overview shows whether the AI is off: a missing key, paused, or the spending cap reached. |
| A social post says Failed | The reason is on the story page. Check the account under Social accounts and click Test. See SOCIAL-SETUP.md. |

To see what the server is doing:
```
cd /opt/newsroom && sudo docker compose logs --tail 100 worker
```

## Private repository

If your repository is private, the server needs permission to download it:

1. On GitHub: your photo → **Settings** → **Developer settings** → **Personal access tokens** → **Fine-grained tokens** → **Generate new token**. Give it access to just the `newsroom` repository, with **Contents: Read-only**.
2. In the step 6 command, use `https://YOURTOKEN@github.com/YOURNAME/newsroom.git` as `REPO_URL`. Replace the `curl …` part with `curl -fsSL -H "Authorization: token YOURTOKEN" https://raw.githubusercontent.com/YOURNAME/newsroom/main/deploy/install.sh`.

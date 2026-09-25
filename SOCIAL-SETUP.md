# Connecting your social accounts

Your newsroom posts published stories to Facebook, Instagram, Threads, Bluesky and X. Each platform needs a one-time setup. You paste the result into **Dashboard → Social accounts**, then click **Test**.

Before you start:

- Set your website address in **Settings → Publication and brand → Website address**, for example `https://news.yourtown.com`. Posts link to it, and Instagram and Threads fetch the story image from it.
- Start with **Bluesky**. It takes five minutes and needs no developer account.
- The platforms change their menus often. If a step doesn't match what you see, paste what you see to Claude and ask for help.

---

## Bluesky (5 minutes)

1. Log in to Bluesky with your newsroom's account.
2. Go to **Settings → Privacy and security → App passwords → Add app password**. Name it "Newsroom" and copy the password it shows.
3. In **Social accounts → Bluesky**, fill in:
   - **Handle:** for example `hillsdaledaily.bsky.social`
   - **App password:** the one you just copied
   - **Server:** leave as `https://bsky.social`
4. Click **Save**, then **Test**.

Posts show your text with a link card that has the headline and image.

---

## Facebook Page (20–30 minutes, once)

You need to be an admin of the Page.

1. Go to **developers.facebook.com** and log in with your Facebook account. Click **Get started** if you haven't used it before.
2. Click **My Apps → Create App**.
   - Name it something like "Hillsdale Daily Newsroom".
   - For the use case, choose the one for managing your Page (it's called something like **"Manage everything on your Page"**). If you're asked for an app type, choose **Business**.
3. In the app, add these permissions to the Page use case:
   - `pages_show_list`
   - `pages_read_engagement`
   - `pages_manage_posts`
4. Open **Tools → Graph API Explorer**.
   - Choose your app.
   - Click **Generate Access Token**, log in, and allow access to your Page.
5. Make the token long-lived:
   - Open **Tools → Access Token Debugger** and paste the token.
   - Click **Extend Access Token** and copy the new, long-lived token.
6. Back in the Graph API Explorer:
   - Paste the long-lived token into the token box.
   - Request `me/accounts` and click **Submit**.
   - Find your Page in the result. Copy its `id` (the **Page ID**) and its `access_token` (the **Page access token**). A Page token made this way doesn't expire.
7. In **Social accounts → Facebook Page**, paste the Page ID and Page access token. Click **Save**, then **Test**.

While your app is in "development mode", it can still post to Pages you manage. You don't need App Review to post to your own Page.

---

## Instagram (10 minutes, after Facebook)

Instagram posting goes through the same Meta app.

1. Your Instagram account must be a **Professional account** (Business or Creator) that is **linked to your Facebook Page**. Set this up in the Instagram app: **Settings → Account type and tools**. Link the Page under **Accounts Center**.
2. In your Meta app, add the Instagram permissions:
   - `instagram_basic`
   - `instagram_content_publish`
3. In the Graph API Explorer:
   - Generate a new user token that includes those permissions, and make it long-lived as in Facebook step 5.
   - Request `me/accounts` again, copy the new Page access token, and paste it into **Facebook Page** in Social accounts.
4. With that Page token, request `YOUR-PAGE-ID?fields=instagram_business_account`. The number it returns is your **Instagram account ID**.
5. In **Social accounts → Instagram**, paste the Instagram account ID. Leave the token blank: it uses the Facebook Page token. Click **Save**, then **Test**.

Instagram doesn't allow clickable links in posts, so captions end with "Full story: link in bio". Put your website in your Instagram bio.

---

## Threads (15 minutes)

1. On **developers.facebook.com**, create a new app (or add a use case to your existing one) and choose **"Access the Threads API"**.
2. In that use case, add the permissions:
   - `threads_basic`
   - `threads_content_publish`
3. Under **App roles → Roles**, add your Threads account as a **Threads Tester**. Accept the invitation in the Threads app: **Settings → Account → Website permissions → Invites**.
4. In the Threads use case settings, use the **User Token Generator** to create an access token for your account.
5. Find your Threads user ID. Open this address in your browser, with your token at the end:
   `https://graph.threads.net/v1.0/me?fields=id,username&access_token=YOUR-TOKEN`
   It shows your `id`.
6. In **Social accounts → Threads**, paste the user ID and token. Click **Save**, then **Test**.

Threads tokens last 60 days. The newsroom renews yours automatically every week, as long as it keeps working. If Test ever fails, generate a new token and paste it in.

---

## X (15 minutes)

X charges for most API access. Check the current plans at **developer.x.com**. At the time of writing, the free level allows a small number of posts per month, which may be enough for a local site.

1. Go to **developer.x.com**, sign in with your newsroom's X account, and sign up for a developer account.
2. Create a **Project** and an **App** inside it.
3. In the app, open **User authentication settings**.
   - Set **App permissions** to **Read and write**.
   - Choose **Web App, Automated App or Bot**.
   - For the callback and website URLs, use your website address.
4. Open **Keys and tokens**. Copy the **API Key** and **API Key Secret**.
5. Generate an **Access Token and Secret**. Do this after setting Read and write, or they won't be able to post.
6. In **Social accounts → X**, paste all four. Click **Save**, then **Test**.

Posts are your text plus the link. X shows the headline and image as a card.

---

## Using it

On each story's page, **Where to share** lists your connected platforms.

1. Tick the ones you want and edit the text for each. The counter shows each platform's limit.
2. Publish the story. Posts go out within a minute.
3. The story page then shows **Posted** with a link to each post, or **Failed** with the reason and a **Try again** button.

Stories that publish automatically, such as weather alerts, use the defaults at the bottom of Social accounts. Wire stories are never shared automatically.

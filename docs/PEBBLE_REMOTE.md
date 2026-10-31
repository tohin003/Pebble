# Pebble Remote: use Pebble on your Mac from your iPhone

Pebble Remote is an iPhone app that controls Pebble on your Mac: chats, runs, approvals, settings and every button in
Pebble's window. Your Mac is the only server. Pebble Remote talks to Pebble directly, over your home Wi-Fi or over your
own private network when you are away. No server of Pebble's or anyone else's sits in between.

## What you need

- Pebble 2.0 or later on a Mac.
- An iPhone with Pebble Remote installed. Pebble Remote won't be on the App Store: it will be offered only as source
  code, which you build and install with Xcode, and that code is not published yet.
- For use away from home: [Tailscale](https://tailscale.com) on the Mac and on the iPhone. Its personal plan is free.
  Other options are listed under "Away from home without Tailscale" below.

## 1. Turn on Remote

In Pebble, open **Settings › iPhone** and turn **Pebble Remote** on. It is off by default; while it is off, Pebble opens no
network port at all.

The page has three more choices:

- **Listen on this Wi-Fi network:** lets your iPhone reach the Mac directly at home. Turn it off on networks you don't
  trust, such as a café's Wi-Fi; your iPhone then reaches this Mac through Tailscale only.
- **Keep this Mac awake:** while Pebble Remote is on, the Mac stays awake, on battery too, so your iPhone can reach it.
  The screen can still sleep. A MacBook still sleeps when you close its lid, unless it is on power with an external
  display.
- **After Face ID, your iPhone can act for:** how long one Face ID check lets your iPhone act before it asks again.

The first time, macOS may ask whether Pebble may accept incoming connections and find devices on your local network.
Allow both. Pebble Remote needs them to reach the Mac.

## 2. Pair your iPhone (once, at home)

1. Put the iPhone on the same Wi-Fi as the Mac.
2. In **Settings › iPhone**, choose **Show a pairing code**. A pairing code works once, for 5 minutes.
3. In Pebble Remote, scan the QR code (or paste the link from **Copy link**).
4. The Mac shows six digits and the iPhone's name. Choose **They match: pair** only if your iPhone shows the same six
   digits. This stops anyone who got hold of your link from pairing their own phone.

Your iPhone now appears under **Paired iPhones**. **Remove** cuts it off at once: its connection closes and its keys
stop working.

## 3. Away from home: Tailscale

Tailscale gives your own devices a private, encrypted network between them. Pebble Remote uses it to reach your Mac from
anywhere: mobile data, another Wi-Fi network, another country. Your Mac stays the only server. Tailscale connects the two
devices directly when the networks allow it, and through its relays otherwise. The relays pass on encrypted traffic
they cannot read, and Pebble's own encryption runs inside it as well.

**On the Mac:**

1. Install Tailscale from [tailscale.com/download/mac](https://tailscale.com/download/mac). Install only one Tailscale
   app on a Mac.
2. Open it, allow its network extension and VPN configuration when macOS asks, and log in.
3. In Tailscale's menu, keep **Allow incoming connections** on, and let it start at login.
4. In Tailscale's admin console (login.tailscale.com, **Machines**), open your Mac's **⋯** menu and choose **Disable key
   expiry**. Otherwise the Mac leaves your network after a few months until you log in again, perhaps while you are
   away.

**On the iPhone:**

1. Install Tailscale from the App Store and log in with the **same account** as on the Mac.
2. Allow the VPN configuration iOS asks for.
3. Leave Tailscale switched on. It reconnects by itself after a restart. An iPhone runs one VPN at a time, so if you
   turn on another VPN, switch Tailscale back on afterwards.

Pebble finds the Tailscale address by itself and adds it to every pairing link and to the iPhone's list of ways to reach
the Mac. If you paired before installing Tailscale, Pebble Remote learns the new address the next time it connects at
home.

## Away from home without Tailscale

Pebble never listens on a public internet address, so there is no port to forward. These work:

- **Headscale:** a self-hosted, open-source control server for Tailscale's apps, if you want no company involved. The
  official Tailscale apps for Mac and iPhone can log in to a custom control server.
- **Any VPN** that gives your iPhone a route to the Mac's private address (WireGuard on a home router, for example).
- **Home Wi-Fi only:** pair and use Pebble Remote on the same network, with nothing else installed.

## What your iPhone may and may not do

Your iPhone can do what Pebble's window can, with three limits:

- **Face ID first.** Your iPhone asks for Face ID before it acts. One check covers the time you chose; approvals, risky
  settings and opening things on the Mac's screen ask again.
- **Some things stay on the Mac:** signing in to accounts, credentials, installing code and the Mac's own file pickers.
  Pairing, confirming and removing iPhones also happen only on the Mac.
- **The Mac shows what the phone did.** While a phone is connected, a dot appears in the menu bar. Each action shows in
  **What your iPhone did** and as a note in Pebble's window. Approvals, screenshots and new pairings also post a
  notification.

A screenshot taken from the phone needs Pebble's Screen Recording permission on the Mac. The Mac shows a notice each
time one is taken, and the image is sent to the phone and deleted from the Mac at once.

## How it is secured

- **TLS 1.3, pinned to your Mac's own key.** Pebble Remote accepts no other certificate, from any authority, so a device
  on the same network cannot pretend to be your Mac.
- **Pairing:** a code that works once, for 5 minutes, with 5 tries, plus the six digits you compare on the Mac.
- **Requests:** every action is signed by a key on the iPhone. Each signature is tied to that connection, that request
  and that moment, so it cannot be replayed.
- **Limits** on message sizes, request rates and connections keep a hostile device on the network from wearing the Mac
  down.
- **Logs:** Pebble's logs never record codes, links, keys or what you send.

## Troubleshooting

- **The iPhone can't reach the Mac away from home:**
  - Is Tailscale on and logged in on both devices, with the same account?
  - Is the Mac awake, with Pebble open and Pebble Remote on?
  - Is "Allow incoming connections" on in the Mac's Tailscale menu?
- **macOS asked about incoming connections or the local network, and you chose Don't Allow:** change it in System
  Settings › Network › Firewall, or in Privacy & Security › Local Network, then turn Pebble Remote off and on.
- **A pairing code was refused:** codes expire after 5 minutes and work once. Show a new one.
- **You lost your iPhone:** remove it in **Settings › iPhone** on the Mac. It is cut off at once.

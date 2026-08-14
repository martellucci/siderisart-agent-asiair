# Reaching a remote ASIAIR

***English** · [Italiano](it/RETE.md)*

If your ASIAIR is in the back garden, skip this page: it's already on your
network. If it lives in a remote observatory a few thousand kilometres away,
there is exactly one problem to solve — **make the server see the ASIAIR by IP
address**, as if it were in the next room.

> The addresses used below (`192.0.2.x`, `198.51.100.x`, `203.0.113.x`) are
> reserved for documentation: **replace them with your own**.

---

## The principle: the VPN belongs on the router, not on the server

The tempting approach is to let the server running the agent handle the VPN.
It's the worst option: if the tunnel drops, the agent drops with it; if you
restart the agent, you renegotiate the tunnel; and every other device in the
house — the tablet running the ASIAIR app, say — is left outside.

**Put it on the router**, always up. The server doesn't even know it exists: to
it, the ASIAIR is simply a reachable IP. It's the only arrangement that survives
months without maintenance.

```
   ┌───────────────┐        ┌──────────────┐   tunnel    ┌──────────────┐
   │ agent server  │───────►│  THE ROUTER  │═══════════► │ remote net   │
   │ (astro VLAN)  │        │  VPN client  │             │    ASIAIR    │
   └───────────────┘        │  always up   │             └──────────────┘
   ┌───────────────┐        └──────────────┘
   │ tablet, app   │───────►
   └───────────────┘
```

---

## 1. A dedicated VLAN

Put the astro server (and the devices you run the app from) on a **separate
virtual network** from the rest of the house. It costs five minutes and saves you
from some random cheap camera finding an open route to your observatory.

- Name it `ASTRO`, any VLAN ID.
- Dedicated subnet, with a **static IP or reservation** for the server.
- Only the server and the app devices on this VLAN.

> ⚠️ **Blocking prerequisite**: ask whoever runs the observatory for the
> **remote subnet**. If it collides with yours — the classic factory
> `192.168.1.x` on both ends — routing **cannot work**: you must renumber your
> own LAN before doing anything else.

## 2. The VPN client on the router

Essentially every serious router (UniFi, OPNsense, MikroTik, pfSense) can act as
an OpenVPN or WireGuard **client** towards a remote network.

- Load the profile the observatory gives you (`.ovpn` or WireGuard).
- If the profile uses `auth-user-pass`, enter the username and password.
- Check that the status reads **Connected** and that the client reconnects on its
  own.

## 3. Split tunnel: only what belongs in the tunnel

**Do not push all your internet traffic through the tunnel.** You'd be routing
your household traffic across the world, at the observatory's latency and
bandwidth.

Create a routing rule:

- **source**: the `ASTRO` VLAN (or just the server);
- **destination**: the observatory's **remote subnets**;
- **interface**: the VPN tunnel.

Everything else — smart-plug cloud, Telegram, updates, the roof API — keeps going
out over the normal WAN. That's also why powering the plugs and reading the roof
**keep working when the VPN is down**: they go over the public internet, not
through the tunnel.

## 4. Firewall: open the minimum

From the `ASTRO` VLAN to the remote subnet, through the tunnel:

| Protocol | Why |
|---|---|
| **ICMP** | reachability diagnostics |
| **TCP 4700** | ASIAIR imager/plan channel |
| **TCP 4400** | ASIAIR guider/mount channel |
| **TCP 445** | SMB, downloading the images |

Then: `ASTRO → Internet` allowed (plug cloud, Telegram, updates) and
`ASTRO → other local VLANs` **blocked**. Return traffic is handled by the
stateful firewall; you don't need to open it.

> The ports used by the app's live streams aren't documented. If some app feature
> misbehaves, temporarily open **all TCP to the ASIAIR's IP only**, watch with
> Wireshark which ports it actually uses, then close it back down. The agent
> needs 4700, 4400 and 445.

## 5. Getting in from outside the house

If you want to reach the remote ASIAIR **from your phone while you're out**,
without toggling between two VPNs every time, the trick is to route your home
tunnel through the observatory one:

1. in the route towards the remote network, include your **VPN server's address
   pool** among the sources (or set the source to "Any");
2. allow **VPN zone to VPN zone** traffic in the firewall, towards the remote
   subnets;
3. on the phone, add the **remote subnets** to the WireGuard profile's
   `AllowedIPs`: the one your router generates only contains local networks;
4. NAT is usually already fine (the router masquerades outbound on the tunnel);
   if it isn't, add masquerade with the VPN pool as source.

> **Tunnel inside a tunnel**: if SMB or the app stall on large transfers, the
> culprit is almost always MTU. Drop the phone profile's to **1280**.

---

## 6. Verify

From the server:

```bash
ping <ASIAIR_IP>
nc -vz <ASIAIR_IP> 4700
nc -vz <ASIAIR_IP> 445
mount -t cifs "//<ASIAIR_IP>/TF Images" /mnt/test -o credentials=...,ro
```

---

## 7. "Is the VPN up?" — the check that looks obvious and isn't

The instinctive approach is to ping a host on the remote network and conclude:
answers = tunnel up, no answer = tunnel down. **That's wrong**, and it will give
you false alarms constantly.

When the rig is powered off, **nothing on the remote network answers** — not the
ASIAIR, not whichever probe host you picked, often not even the gateway. The ping
fails and you conclude "VPN down" while the tunnel is perfectly healthy.

The real evidence is in a detail: when you ping the powered-off ASIAIR, it's the
**remote router** that replies `Destination Host Unreachable`. That ICMP packet
came back *through the tunnel* — its very existence proves the VPN is working,
and as a bonus it tells you the box is simply switched off.

That's the logic in `vpn_diagnose()` in `sfro_agent.py`:

1. ping the ASIAIR;
2. it answers → VPN up, box on;
3. an ICMP *unreachable* arrives from an address on the remote network → **VPN up,
   box off** (the normal daytime state);
4. only if nothing at all comes back, try the fallback probe
   (`asiair.vpn_probe_host`, typically the remote router);
5. total silence → VPN down, and now it's worth raising an alarm.

Without that distinction the agent wakes you every morning to report that the VPN
has dropped, when all it did was power off the rig exactly as you asked.

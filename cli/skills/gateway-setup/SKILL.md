---
name: gateway-setup
description: "Connect the claw gateway to a messaging platform (WeChat, Telegram, Feishu)"
platforms: [linux, macos, windows]
---

# Gateway Setup Guide

The claw gateway connects the AI agent to messaging platforms so
you can interact with it through WeChat, Telegram, or Feishu.

## Step 1: Check current gateway status

Run:

```
claw gateway status
```

This shows which platforms are configured and whether MCP servers are running.

## Step 2: Add a messaging platform

### WeChat (Weixin)

```
claw gateway add weixin
```

Follow the QR code prompt to scan with your phone.

### Telegram (planned)

```
claw gateway add telegram
```

### Feishu / Lark (planned)

```
claw gateway add feishu
```

## Step 3: Configure MCP servers

```
claw gateway mcp
```

Select the servers you want the gateway to manage.
Available: SQLite, GitHub.

## Step 4: Start the gateway

```
claw gateway start
```

Add ``--verbose`` to see detailed logs.

## Step 5: Verify

Check the gateway status:

```
claw gateway status
```

The running platform and MCP servers should show as active.
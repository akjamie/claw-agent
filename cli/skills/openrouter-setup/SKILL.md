---
name: openrouter-setup
description: "Get an API key from OpenRouter and configure it as your inference provider"
platforms: [linux, macos, windows]
---

# OpenRouter Setup Guide

OpenRouter is an aggregator that provides access to 200+ models
through a single API endpoint and billing system.

## Step 1: Get an API key

1. Go to https://openrouter.ai/keys
2. Sign in or create an account
3. Click **Create Key**
4. Copy the key (starts with `sk-or-v1-`)

## Step 2: Configure Claw

Run:

```
claw models
```

Select **OpenRouter** as the provider, paste your API key,
and choose a model from the available list.

## Step 3: Verify

Check your configuration with:

```
claw models --show
```

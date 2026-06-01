---
name: nous-setup
description: "Configure Nous Portal as your inference provider"
platforms: [linux, macos, windows]
---

# Nous Portal Setup Guide

Nous Portal provides access to Nous Research's latest models
through a subscription-based API.

## Step 1: Get an API key

1. Go to https://portal.nousresearch.com
2. Sign in or create an account
3. Navigate to API settings
4. Copy your API key

## Step 2: Configure Claw

Run:

```
claw models
```

Select **Nous Portal** as the provider, paste your API key,
and choose a model.

## Step 3: Verify

Check your configuration with:

```
claw models --show
```

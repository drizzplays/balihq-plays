import discord
from discord.ext import commands
import os

# 1. Setup Intents (Required for modern Discord bots)
intents = discord.Intents.default()
intents.message_content = True  # Allows bot to read commands

# 2. Define the Bot instance
bot = commands.Bot(command_prefix="!", intents=intents)

# 3. Profile Update Command
@bot.command()
async def update_profile(ctx):
    """Updates the bot's avatar and banner from the /images folder"""
    avatar_path = "images/avatar.png"
    banner_path = "images/banner.png"

    try:
        # Update Avatar
        if os.path.exists(avatar_path):
            with open(avatar_path, "rb") as f:
                await bot.user.edit(avatar=f.read())
            await ctx.send("✅ Avatar updated successfully.")
        else:
            await ctx.send("❌ Avatar file not found in /images.")

        # Update Banner (Note: Requires specific bot permissions/tier)
        if os.path.exists(banner_path):
            with open(banner_path, "rb") as f:
                await bot.user.edit(banner=f.read())
            await ctx.send("✅ Banner updated successfully.")
        
    except discord.HTTPException as e:
        await ctx.send(f"⚠️ Discord API error: {e}")
    except Exception as e:
        await ctx.send(f"⚠️ General error: {e}")

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name} (ID: {bot.user.id})')
    print('------')

# 4. Run the Bot using Environment Variables (Best practice for CI/CD)
token = os.getenv('BOT_TOKEN')

if __name__ == "__main__":
    if token:
        bot.run(token)
    else:
        print("CRITICAL ERROR: 'BOT_TOKEN' environment variable not found.")

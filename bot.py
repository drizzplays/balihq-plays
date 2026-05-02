import discord
from discord.ext import commands
import os

# Assuming your bot/client is already defined
# bot = commands.Bot(command_prefix="!")

@bot.command()
async def update_profile(ctx):
    # Define paths to your images
    avatar_path = "images/avatar.png"
    banner_path = "images/banner.png"

    try:
        # Update Avatar
        if os.path.exists(avatar_path):
            with open(avatar_path, "rb") as avatar_file:
                new_avatar = avatar_file.read()
                await bot.user.edit(avatar=new_avatar)
            print("Avatar updated successfully.")

        # Update Banner
        # NOTE: Only 'Verified' bots or bots in specific Nitro-boosted 
        # servers can usually have banners changed via API
        if os.path.exists(banner_path):
            with open(banner_path, "rb") as banner_file:
                new_banner = banner_file.read()
                await bot.user.edit(banner=new_banner)
            print("Banner updated successfully.")
            
        await ctx.send("Profile updated with local assets.")

    except Exception as e:
        print(f"Error updating profile: {e}")

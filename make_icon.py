from PIL import Image, ImageDraw

SIZE = 256
img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
draw = ImageDraw.Draw(img)
draw.ellipse((8, 8, SIZE - 8, SIZE - 8), fill=(30, 30, 34, 255), outline=(235, 235, 235, 255), width=12)
draw.ellipse((80, 80, SIZE - 80, SIZE - 80), fill=(225, 45, 45, 255))
img.save("icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("icon.ico written")

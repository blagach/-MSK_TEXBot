"""Annotate supplied real phone captures; never redraw Telegram or modify sources.

Run locally with Pillow: python scripts/build_help_illustrations.py SOURCE_DIR...
Only new, flattened, metadata-free PNG files are written. Phone/address regions
are irreversibly masked before saving; original captures stay outside Git.
"""
import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


# Crop, privacy masks and highlighted controls refer to original capture pixels.
# No missing screen is replaced by a mock or an unrelated illustration.
ILLUSTRATIONS = (
    ('customer-1.png', 'IMG_1447.jpeg', (0, 1290, 709, 1500), (),
     ((24, 1315, 355, 1396),), 'Создайте заявку', 'В меню заказчика нажмите «Оставить заявку».'),
    ('customer-3.png', 'IMG_1444.jpeg', (0, 450, 709, 1405),
     ((120, 647, 600, 691), (150, 1135, 590, 1178)),
     ((18, 1187, 638, 1258), (18, 1261, 638, 1331)),
     'Проверьте все поля', 'Отправьте на модерацию или измените данные.'),
    ('customer-4.png', 'IMG_1454.jpeg', (0, 270, 709, 1480), (),
     ((403, 335, 692, 402), (18, 462, 640, 535)),
     'Следите за статусом', 'Откройте «Мои заявки» и выберите свою заявку.'),
    ('customer-5.png', 'IMG_1452.jpeg', (0, 681, 709, 1116), (),
     ((18, 897, 641, 970),), 'Закрытие требует подтверждения',
     'Причину можно пропустить. Затем подтвердите закрытие.'),
    ('customer-6.png', 'IMG_1451.jpeg', (0, 1225, 709, 1485), (),
     ((24, 1324, 352, 1403), (356, 1324, 684, 1403)),
     'Продолжите или отмените черновик', 'Отмена требует подтверждения и сохраняет данные в архиве.'),
    ('provider-4.png', 'IMG_1449.jpeg', (0, 334, 709, 994),
     ((119, 455, 596, 496), (280, 869, 595, 909), (252, 931, 539, 981)),
     ((18, 917, 636, 991),), 'Контакт в карточке заявки',
     'Кнопка с номером копирует его. Анкета отклика не нужна.'),
    ('provider-6.png', 'IMG_1450.jpeg', (0, 987, 709, 1494), (),
     ((24, 1320, 683, 1396),), 'Управляйте уведомлениями',
     'Кнопка показывает текущее состояние уведомлений.'),
)


def build(directories, output):
    font_path = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
    title_font = ImageFont.truetype(font_path, 29)
    body_font = ImageFont.truetype(font_path, 20)
    mask_font = ImageFont.truetype(font_path, 19)
    output.mkdir(parents=True, exist_ok=True)
    for name, source_name, crop, masks, highlights, title, body in ILLUSTRATIONS:
        source = next((folder / source_name for folder in directories if (folder / source_name).is_file()), None)
        if source is None:
            raise FileNotFoundError(source_name)
        destination = output / name
        if destination.exists():
            raise FileExistsError(destination)
        with Image.open(source) as original:
            if original.size != (709, 1536):
                raise ValueError('Unexpected capture dimensions')
            capture = original.convert('RGB')
        draw = ImageDraw.Draw(capture)
        for rectangle in masks:
            draw.rectangle(rectangle, fill='#243044')
            draw.text((rectangle[0] + 8, rectangle[1] + 6), 'Скрыто в инструкции', fill='white', font=mask_font)
        for rectangle in highlights:
            draw.rounded_rectangle(rectangle, radius=10, outline='#ff8a00', width=6)
        cropped = capture.crop(crop)
        page = Image.new('RGB', (749, cropped.height + 173), '#f5f6f8')
        draw = ImageDraw.Draw(page)
        draw.text((20, 13), title, fill='#172237', font=title_font)
        draw.text((20, 54), body, fill='#263348', font=body_font)
        draw.text((20, 84), 'Реальный экран тестового бота · личные данные скрыты', fill='#586375', font=body_font)
        page.paste(cropped, (20, 122))
        draw.text((20, page.height - 33), 'Оранжевая рамка выделяет нужное действие.', fill='#586375', font=body_font)
        # A new raster contains only rendered pixels, no original EXIF or file bytes.
        with destination.open('xb') as stream:
            page.quantize(colors=128).save(stream, format='PNG', optimize=True)
        print(destination)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path, nargs='+')
    parser.add_argument('--output', type=Path, default=Path('assets/help'))
    args = parser.parse_args()
    build(args.source, args.output)

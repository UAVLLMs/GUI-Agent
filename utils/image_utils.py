"""
图像处理工具函数

提供图像与 base64 编码之间的转换功能。
"""

import io
import base64
from typing import Optional
from PIL import Image


def encode_image_to_base64(
    image: Image.Image, 
    image_format: str = "PNG",
    include_data_prefix: bool = True
) -> str:
    """
    将 PIL Image 编码为 base64 字符串
    
    Args:
        image: PIL Image 对象
        image_format: 图片格式（默认 PNG，支持 PNG, JPEG, WEBP 等）
        include_data_prefix: 是否包含 data:image/xxx;base64, 前缀
        
    Returns:
        base64 编码的字符串
        
    Example:
        >>> from PIL import Image
        >>> img = Image.new('RGB', (100, 100), color='red')
        >>> base64_str = encode_image_to_base64(img)
        >>> print(base64_str[:50])  # data:image/png;base64,iVBORw0KGgo...
    """
    buffered = io.BytesIO()
    image.save(buffered, format=image_format)
    base64_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
    
    if include_data_prefix:
        return f"data:image/{image_format.lower()};base64,{base64_str}"
    else:
        return base64_str


def decode_base64_to_image(
    base64_str: str,
    mode: Optional[str] = "RGB"
) -> Image.Image:
    """
    将 base64 字符串解码为 PIL Image
    
    Args:
        base64_str: base64 编码的字符串，可以带或不带 data:image/xxx;base64, 前缀
        mode: 图片模式（如 "RGB", "RGBA"），None 表示保持原模式
        
    Returns:
        PIL Image 对象
        
    Example:
        >>> from PIL import Image
        >>> img = Image.new('RGB', (100, 100), color='red')
        >>> base64_str = encode_image_to_base64(img)
        >>> decoded_img = decode_base64_to_image(base64_str)
        >>> print(decoded_img.size)  # (100, 100)
    """
    # 处理 data:image/xxx;base64, 前缀
    if base64_str.startswith("data:image"):
        base64_str = base64_str.split(",", 1)[-1]
    
    image_bytes = base64.b64decode(base64_str)
    image = Image.open(io.BytesIO(image_bytes))
    
    if mode:
        image = image.convert(mode)
    
    return image


def encode_image_url(image: Image.Image, image_format: str = "PNG") -> str:
    """
    将 PIL Image 编码为 OpenAI API 格式的 image_url
    
    这是一个便捷函数，返回符合 OpenAI messages 格式的 image_url 结构。
    
    Args:
        image: PIL Image 对象
        image_format: 图片格式（默认 PNG）
        
    Returns:
        base64 编码的图片 URL 字符串
        
    Example:
        >>> from PIL import Image
        >>> img = Image.new('RGB', (100, 100), color='red')
        >>> image_url = encode_image_url(img)
        >>> message = {
        ...     "role": "user",
        ...     "content": [{"type": "image_url", "image_url": {"url": image_url}}]
        ... }
    """
    return encode_image_to_base64(image, image_format, include_data_prefix=True)
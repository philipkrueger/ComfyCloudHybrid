"""object_info-style schema fixtures mirroring the real cloud catalog shape."""

CLOUD_OBJECT_INFO = {
    "PrimitiveStringMultiline": {
        "input": {"required": {"value": ["STRING", {"multiline": True}]}},
        "output": ["STRING"],
    },
    "PreviewAny": {
        "input": {"required": {"source": ["*", {}]}},
        "output": ["STRING"],
        "output_node": True,
    },
    "LoadAudio": {
        "input": {"required": {"audio": ["COMBO", {"audio_upload": True,
                                                   "options": []}]}},
        "output": ["AUDIO"],
    },
    "SaveAudio": {
        "input": {"required": {"audio": ["AUDIO", {}],
                               "filename_prefix": ["STRING", {}]}},
        "output_node": True,
    },
    "FakeAudioProc": {
        "input": {"required": {"audio": ["AUDIO", {}]}},
        "output": ["AUDIO"],
    },
    "GeminiImageNode": {
        "api_node": True,
        "input": {
            "required": {
                "prompt": ["STRING", {"multiline": True}],
                "model": [["gemini-2.5-flash-image", "gemini-3"], {}],
                "seed": ["INT", {"default": 0, "min": 0, "max": 18446744073709551615,
                                 "control_after_generate": True}],
            },
            "optional": {
                "images": ["IMAGE", {}],
                "files": ["GEMINI_INPUT_FILES", {}],
                "aspect_ratio": [["auto", "1:1", "9:16"], {}],
                "response_modalities": [["IMAGE+TEXT", "IMAGE"], {}],
                "system_prompt": ["STRING", {"multiline": True}],
            },
        },
    },
    "ImageBatch": {
        "input": {"required": {"image1": ["IMAGE", {}], "image2": ["IMAGE", {}]}},
    },
    "LoadImage": {
        "input": {"required": {
            "image": [["Reference_9x16_2.png"], {"image_upload": True}]}},
    },
    "SaveImage": {
        "input": {"required": {
            "images": ["IMAGE", {}],
            "filename_prefix": ["STRING", {"default": "ComfyUI"}]}},
    },
    "ImageToMask": {
        "input": {"required": {
            "image": ["IMAGE", {}],
            "channel": [["red", "green", "blue", "alpha"], {}]}},
    },
    "FakeProc": {
        "input": {
            "required": {
                "image": ["IMAGE", {}],
                "strength": ["FLOAT", {"default": 1.0}]},
            "optional": {
                "image_b": ["IMAGE", {}]},
        },
    },
    "PrimitiveInt": {
        "input": {"required": {
            "value": ["INT", {"default": 0, "min": -2147483648, "max": 2147483647,
                              "control_after_generate": True}]}},
    },
    "CreateVideo": {
        "input": {"required": {"images": ["IMAGE", {}], "fps": ["FLOAT", {"default": 24.0}]}},
    },
    "SaveVideo": {
        "input": {"required": {
            "video": ["VIDEO", {}],
            "filename_prefix": ["STRING", {"default": "video/ComfyUI"}],
            "format": [["auto", "mp4"], {"default": "auto"}],
            "codec": [["auto", "h264"], {"default": "auto"}]}},
        "output_node": True,
    },
}

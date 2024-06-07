import os
import re

import yaml
import torch
import torchaudio
from torch import autocast

import audioldm2.latent_diffusion.modules.phoneme_encoder.text as text
from audioldm2.utilities.audio import wav_to_fbank, TacotronSTFT
from audioldm2.latent_diffusion.models.ddpm import LatentDiffusion
from audioldm2.latent_diffusion.models.ddim import DDIMSampler
from audioldm2.latent_diffusion.util import get_vits_phoneme_ids_no_padding

from audioldm2.utils import default_audioldm_config, download_checkpoint, get_bit_depth, get_duration
from audioldm2.utilities.audio.stft import TacotronSTFT
from audioldm2.utilities.audio.tools import wav_to_fbank
from einops import repeat
import os

# CACHE_DIR = os.getenv(
#     "AUDIOLDM_CACHE_DIR", os.path.join(os.path.expanduser("~"), ".cache/audioldm2")
# )

def seed_everything(seed):
    import random, os
    import numpy as np
    import torch

    random.seed(seed)
    os.environ["PYTHONHASHSEED"] = str(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = True

def text2phoneme(data):
    return text._clean_text(re.sub(r'<.*?>', '', data), ["english_cleaners2"])

def text_to_filename(text):
    return text.replace(" ", "_").replace("'", "_").replace('"', "_")

def set_cond_text(latent_diffusion):
    latent_diffusion.cond_stage_key = "text"
    latent_diffusion.clap.embed_mode="text"
    return latent_diffusion

def set_cond_audio(latent_diffusion):
    latent_diffusion.cond_stage_key = "waveform"
    latent_diffusion.clap.embed_mode="audio"
    return latent_diffusion

def extract_kaldi_fbank_feature(waveform, sampling_rate, log_mel_spec):
    norm_mean = -4.2677393
    norm_std = 4.5689974

    if sampling_rate != 16000:
        waveform_16k = torchaudio.functional.resample(
            waveform, orig_freq=sampling_rate, new_freq=16000
        )
    else:
        waveform_16k = waveform

    waveform_16k = waveform_16k - waveform_16k.mean()
    fbank = torchaudio.compliance.kaldi.fbank(
        waveform_16k,
        htk_compat=True,
        sample_frequency=16000,
        use_energy=False,
        window_type="hanning",
        num_mel_bins=128,
        dither=0.0,
        frame_shift=10,
    )

    TARGET_LEN = log_mel_spec.size(0)

    # cut and pad
    n_frames = fbank.shape[0]
    p = TARGET_LEN - n_frames
    if p > 0:
        m = torch.nn.ZeroPad2d((0, 0, 0, p))
        fbank = m(fbank)
    elif p < 0:
        fbank = fbank[:TARGET_LEN, :]

    fbank = (fbank - norm_mean) / (norm_std * 2)

    return {"ta_kaldi_fbank": fbank}  # [1024, 128]

def make_batch_for_text_to_audio(text, transcription="", waveform=None, fbank=None, batchsize=1):
    text = [text] * batchsize
    if(transcription):
        transcription = text2phoneme(transcription)
    transcription = [transcription] * batchsize

    if batchsize < 1:
        print("Warning: Batchsize must be at least 1. Batchsize is set to .")

    if fbank is None:
        fbank = torch.zeros(
            (batchsize, 1024, 64)
        )  # Not used, here to keep the code format
    else:
        fbank = torch.FloatTensor(fbank)
        fbank = fbank.expand(batchsize, 1024, 64)
        assert fbank.size(0) == batchsize

    stft = torch.zeros((batchsize, 1024, 512))  # Not used
    phonemes = get_vits_phoneme_ids_no_padding(transcription)

    if waveform is None:
        waveform = torch.zeros((batchsize, 160000))  # Not used
        ta_kaldi_fbank = torch.zeros((batchsize, 1024, 128))
    else:
        waveform = torch.FloatTensor(waveform)
        waveform = waveform.expand(batchsize, -1)
        assert waveform.size(0) == batchsize
        ta_kaldi_fbank = extract_kaldi_fbank_feature(waveform, 16000, fbank)

    batch = {
        "text": text,  # list
        "fname": [text_to_filename(t) for t in text],  # list
        "waveform": waveform,
        "stft": stft,
        "log_mel_spec": fbank,
        "ta_kaldi_fbank": ta_kaldi_fbank,
    }
    batch.update(phonemes)
    return batch


def round_up_duration(duration):
    return int(round(duration / 2.5) + 1) * 2.5


# def split_clap_weight_to_pth(checkpoint):
#     if os.path.exists(os.path.join(CACHE_DIR, "clap.pth")):
#         return
#     print("Constructing the weight for the CLAP model.")
#     include_keys = "cond_stage_models.0.cond_stage_models.0.model."
#     new_state_dict = {}
#     for each in checkpoint["state_dict"].keys():
#         if include_keys in each:
#             new_state_dict[each.replace(include_keys, "module.")] = checkpoint[
#                 "state_dict"
#             ][each]
#     torch.save({"state_dict": new_state_dict}, os.path.join(CACHE_DIR, "clap.pth"))


def build_model(ckpt_path=None, config=None, device=None, model_name="audioldm2-full"):

    if device is None or device == "auto":
        if torch.cuda.is_available():
            device = torch.device("cuda:0")
        elif torch.backends.mps.is_available():
            device = torch.device("mps")
        else:
            device = torch.device("cpu")

    print("Loading AudioLDM-2: %s" % model_name)
    print("Loading model on %s" % device)

    ckpt_path = download_checkpoint(model_name)

    if config is not None:
        assert type(config) is str
        config = yaml.load(open(config, "r"), Loader=yaml.FullLoader)
    else: 
        config = default_audioldm_config(model_name)

    # # Use text as condition instead of using waveform during training
    config["model"]["params"]["device"] = device
    # config["model"]["params"]["cond_stage_key"] = "text"

    # No normalization here
    latent_diffusion = LatentDiffusion(**config["model"]["params"])

    resume_from_checkpoint = ckpt_path

    checkpoint = torch.load(resume_from_checkpoint, map_location=device)

    latent_diffusion.load_state_dict(checkpoint["state_dict"])
    
    latent_diffusion.eval()
    latent_diffusion = latent_diffusion.to(device)
    
    return latent_diffusion


def text_to_audio(
    latent_diffusion,
    text,
    transcription="",
    seed=42,
    ddim_steps=200,
    duration=10,
    batchsize=1,
    guidance_scale=3.5,
    n_candidate_gen_per_text=3,
    latent_t_per_second=25.6,
    config=None,
):

    seed_everything(int(seed))
    waveform = None

    batch = make_batch_for_text_to_audio(text, transcription=transcription, waveform=waveform, batchsize=batchsize)

    latent_diffusion.latent_t_size = int(duration * latent_t_per_second)

    with torch.no_grad():
        waveform = latent_diffusion.generate_batch(
            batch,
            unconditional_guidance_scale=guidance_scale,
            ddim_steps=ddim_steps,
            n_gen=n_candidate_gen_per_text,
            duration=duration,
        )

    return waveform

def style_transfer(
    latent_diffusion,
    text,
    original_audio_file_path,
    transfer_strength,
    seed=42,
    duration=10,
    batchsize=1,
    guidance_scale=2.5,
    ddim_steps=200,
    config=None,
    latent_t_per_second=25.6,
):
    
    if torch.cuda.is_available():
        device = torch.device("cuda:0")
    elif torch.backends.mps.is_available():
        device = torch.device("mps")
    else:
        device = torch.device("cpu")

    assert original_audio_file_path is not None, "You need to provide the original audio file path"
    
    audio_file_duration = get_duration(original_audio_file_path)
    
    assert get_bit_depth(original_audio_file_path) == 16, "The bit depth of the original audio file %s must be 16" % original_audio_file_path
    
    # if(duration > 20):
    #     print("Warning: The duration of the audio file %s must be less than 20 seconds. Longer duration will result in Nan in model output (we are still debugging that); Automatically set duration to 20 seconds")
    #     duration = 20
    
    if(duration > audio_file_duration):
        print("Warning: Duration you specified %s-seconds must equal or smaller than the audio file duration %ss" % (duration, audio_file_duration))
        duration = round_up_duration(audio_file_duration)
        print("Set new duration as %s-seconds" % duration)

    # duration = round_up_duration(duration)
    
    latent_diffusion = set_cond_text(latent_diffusion)

    if config is not None:
        assert type(config) is str
        config = yaml.load(open(config, "r"), Loader=yaml.FullLoader)
    else:
        config = default_audioldm_config()

    seed_everything(int(seed))
    latent_diffusion.latent_t_size = int(duration * latent_t_per_second)
    latent_diffusion.cond_stage_key = "text"

    fn_STFT = TacotronSTFT(
        config["preprocessing"]["stft"]["filter_length"],
        config["preprocessing"]["stft"]["hop_length"],
        config["preprocessing"]["stft"]["win_length"],
        config["preprocessing"]["mel"]["n_mel_channels"],
        config["preprocessing"]["audio"]["sampling_rate"],
        config["preprocessing"]["mel"]["mel_fmin"],
        config["preprocessing"]["mel"]["mel_fmax"],
    )

    mel, _, _ = wav_to_fbank(
        original_audio_file_path, target_length=int(duration * 102.4), fn_STFT=fn_STFT
    )
    mel = mel.unsqueeze(0).unsqueeze(0).to(device)
    mel = repeat(mel, "1 ... -> b ...", b=batchsize)
    # mel.unsqueeze(0).repeat(batchsize, 1, 1, 1)
    init_latent = latent_diffusion.get_first_stage_encoding(
        latent_diffusion.encode_first_stage(mel)
    )  # move to latent space, encode and sample
    if(torch.max(torch.abs(init_latent)) > 1e2):
        init_latent = torch.clip(init_latent, min=-10, max=10)
    sampler = DDIMSampler(latent_diffusion)
    sampler.make_schedule(ddim_num_steps=ddim_steps, ddim_eta=1.0, verbose=False)

    t_enc = int(transfer_strength * ddim_steps)
    prompts = text

    with torch.no_grad():
        with autocast(device):
            with latent_diffusion.ema_scope():
                uc = None
                if guidance_scale != 1.0:
                    uc = latent_diffusion.clap.get_unconditional_condition(
                        batchsize
                    )

                c = latent_diffusion.get_learned_conditioning([prompts] * batchsize)
                z_enc = sampler.stochastic_encode(
                    init_latent, torch.tensor([t_enc] * batchsize).to(device)
                )
                samples = sampler.decode(
                    z_enc,
                    c,
                    t_enc,
                    unconditional_guidance_scale=guidance_scale,
                    unconditional_conditioning=uc,
                )
                # x_samples = latent_diffusion.decode_first_stage(samples) # Will result in Nan in output
                # print(torch.sum(torch.isnan(samples)))
                x_samples = latent_diffusion.decode_first_stage(samples)
                # print(x_samples)
                x_samples = latent_diffusion.decode_first_stage(samples[:,:,:-3,:])
                # print(x_samples)
                waveform = latent_diffusion.first_stage_model.decode_to_waveform(
                    x_samples
                )

    return waveform

def super_resolution_and_inpainting(
    latent_diffusion,
    text,
    transcription="",
    original_audio_file_path = None,
    seed=42,
    ddim_steps=200,
    duration=None,
    batchsize=1,
    guidance_scale=2.5,
    n_candidate_gen_per_text=3,
    time_mask_ratio_start_and_end=(0.40, 0.6), # regenerate the 10% to 15% of the time steps in the spectrogram
    # time_mask_ratio_start_and_end=(1.0, 1.0), # no inpainting
    # freq_mask_ratio_start_and_end=(0.75, 1.0), # regenerate the higher 75% to 100% mel bins
    freq_mask_ratio_start_and_end=(1.0, 1.0), # no super-resolution
    latent_t_per_second=25.6,
    config=None,
):
    seed_everything(int(seed))
    if config is not None:
        assert type(config) is str
        config = yaml.load(open(config, "r"), Loader=yaml.FullLoader)
    else:
        config = default_audioldm_config()
    fn_STFT = TacotronSTFT(
        config["preprocessing"]["stft"]["filter_length"],
        config["preprocessing"]["stft"]["hop_length"],
        config["preprocessing"]["stft"]["win_length"],
        config["preprocessing"]["mel"]["n_mel_channels"],
        config["preprocessing"]["audio"]["sampling_rate"],
        config["preprocessing"]["mel"]["mel_fmin"],
        config["preprocessing"]["mel"]["mel_fmax"],
    )
    
    # waveform = read_wav_file(original_audio_file_path, None)
    mel, _, _ = wav_to_fbank(
        original_audio_file_path, target_length=int(duration * 102.4), fn_STFT=fn_STFT
    )
    
    batch = make_batch_for_text_to_audio(text, transcription=transcription, fbank=mel[None,...], batchsize=batchsize)
        
    # latent_diffusion.latent_t_size = duration_to_latent_t_size(duration)
    latent_diffusion = set_cond_text(latent_diffusion)
        
    with torch.no_grad():
        waveform = latent_diffusion.generate_batch_masked(
            batch,
            unconditional_guidance_scale=guidance_scale,
            ddim_steps=ddim_steps,
            n_gen=n_candidate_gen_per_text,
            duration=duration,
            time_mask_ratio_start_and_end=time_mask_ratio_start_and_end,
            freq_mask_ratio_start_and_end=freq_mask_ratio_start_and_end
        )
    return waveform

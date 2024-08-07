#!D:\GitDownload\SupThirdParty\audioldm2\venv\Scripts\python.exe
import os
import torch
import logging
from audioldm2 import text_to_audio, build_model, save_wave, get_time, read_list, super_resolution_and_inpainting
import argparse

os.environ["TOKENIZERS_PARALLELISM"] = "true"
matplotlib_logger = logging.getLogger('matplotlib')
matplotlib_logger.setLevel(logging.WARNING)

parser = argparse.ArgumentParser()

parser.add_argument(
	"-t",
	"--text",
	type=str,
	required=False,
	default="",
	help="Text prompt to the model for audio generation",
)

parser.add_argument(
    "-f",
    "--file_path",
    type=str,
    required=False,
    default=None,
    help="(--mode sr_inpainting): Original audio file for inpainting; Or (--mode generation): the guidance audio file for generating similar audio, DEFAULT None",
)

parser.add_argument(
	"--transcription",
	type=str,
	required=False,
	default="",
	help="Transcription for Text-to-Speech",
)

parser.add_argument(
	"-tl",
	"--text_list",
	type=str,
	required=False,
	default="",
	help="A file that contains text prompt to the model for audio generation",
)

parser.add_argument(
	"-s",
	"--save_path",
	type=str,
	required=False,
	help="The path to save model output",
	default="./output",
)

parser.add_argument(
	"--model_name",
	type=str,
	required=False,
	help="The checkpoint you gonna use",
	default="audioldm_48k",
	choices=["audioldm_48k", "audioldm_16k_crossattn_t5", "audioldm2-full", "audioldm2-music-665k",
	         "audioldm2-full-large-1150k", "audioldm2-speech-ljspeech", "audioldm2-speech-gigaspeech"]
)

parser.add_argument(
	"-d",
	"--device",
	type=str,
	required=False,
	help="The device for computation. If not specified, the script will automatically choose the device based on your environment.",
	default="auto",
)

parser.add_argument(
	"-b",
	"--batchsize",
	type=int,
	required=False,
	default=1,
	help="Generate how many samples at the same time",
)

parser.add_argument(
	"--ddim_steps",
	type=int,
	required=False,
	default=200,
	help="The sampling step for DDIM",
)

parser.add_argument(
	"-gs",
	"--guidance_scale",
	type=float,
	required=False,
	default=3.5,
	help="Guidance scale (Large => better quality and relavancy to text; Small => better diversity)",
)

parser.add_argument(
	"-dur",
	"--duration",
	type=float,
	required=False,
	default=10.0,
	help="The duration of the samples",
)

parser.add_argument(
	"-n",
	"--n_candidate_gen_per_text",
	type=int,
	required=False,
	default=3,
	help="Automatic quality control. This number control the number of candidates (e.g., generate three audios and choose the best to show you). A Larger value usually lead to better quality with heavier computation",
)

parser.add_argument(
	"--seed",
	type=int,
	required=False,
	default=0,
	help="Change this value (any integer number) will lead to a different generation result.",
)

parser.add_argument(
    "--mode",
    type=str,
    required=False,
    default="generation",
    help="{generation,sr_inpainting} generation: text-to-audio generation; sr_inpainting: super resolution inpainting",
    choices=["generation", "sr_inpainting"]
)

args = parser.parse_args()

torch.set_float32_matmul_precision("high")

save_path = os.path.join(args.save_path, get_time())

text = args.text
random_seed = args.seed
duration = args.duration
sample_rate = 16000
latent_t_per_second=25.6

if ("audioldm2" in args.model_name):
	print(
		"Warning: For AudioLDM2 we currently only support 10s of generation. Please use audioldm_48k or audioldm_16k_crossattn_t5 if you want a different duration.")
	duration = 10
if ("48k" in args.model_name):
	sample_rate = 48000
	latent_t_per_second=12.8

guidance_scale = args.guidance_scale
n_candidate_gen_per_text = args.n_candidate_gen_per_text
transcription = args.transcription

if (transcription):
	if "speech" not in args.model_name:
		print(
			"Warning: You choose to perform Text-to-Speech by providing the transcription.However you do not choose the correct model name (audioldm2-speech-gigaspeech or audioldm2-speech-ljspeech).")
		print("Warning: We will use audioldm2-speech-gigaspeech by default")
		args.model_name = "audioldm2-speech-gigaspeech"
	if (not text):
		print(
			"Warning: You should provide text as a input to describe the speaker. Use default (A male reporter is speaking)")
		text = "A female reporter is speaking full of emotion"

os.makedirs(save_path, exist_ok=True)
audioldm2 = build_model(model_name=args.model_name, device=args.device)

if (args.text_list):
	print("Generate audio based on the text prompts in %s" % args.text_list)
	prompt_todo = read_list(args.text_list)
else:
	prompt_todo = [text]

for text in prompt_todo:
	if ("|" in text):
		text, name = text.split("|")
	else:
		name = text[:128]

	if (transcription):
		name += "-TTS-%s" % transcription

	if(args.mode == "generation"):
		waveform = text_to_audio(
            audioldm2,
            text,
            transcription=transcription, # To avoid the model to ignore the last vocab
            seed=random_seed,
            duration=duration,
            guidance_scale=guidance_scale,
            ddim_steps=args.ddim_steps,
            n_candidate_gen_per_text=n_candidate_gen_per_text,
            batchsize=args.batchsize,
            latent_t_per_second=latent_t_per_second
        )
	elif(args.mode == "sr_inpainting"):
		assert args.file_path is not None
		assert os.path.exists(args.file_path), "The original audio file \'%s\' for style transfer does not exist." % args.file_path
		waveform = super_resolution_and_inpainting(
            latent_diffusion=audioldm2,
            text=text,
            transcription=transcription,
            original_audio_file_path=args.file_path,
            seed=random_seed,
            duration=duration,
            guidance_scale=guidance_scale,
            n_candidate_gen_per_text=n_candidate_gen_per_text,
            ddim_steps=args.ddim_steps,
            batchsize=args.batchsize,
            latent_t_per_second=latent_t_per_second
        )
    
	save_wave(waveform, save_path, name=name, samplerate=sample_rate)

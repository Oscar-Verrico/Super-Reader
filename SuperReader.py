import spacy
from TTS.api import TTS
from ReadFile import readfile
from names_dataset import NameDataset, NameWrapper
import jsonlines
import random
import time
import os
import re
import logging
from concurrent.futures import ThreadPoolExecutor
import librosa
import soundfile as sf
from pydub import AudioSegment

# Initialize logging
logging.basicConfig(level=logging.INFO)

# Load SpaCy Model and TTS stuff
nlp = spacy.load("en_core_web_sm")
try:
    print("Loading TTS model...")
    tts_model = TTS(model_name="tts_models/en/ljspeech/tacotron2-DDC", gpu=False)
    print("Models loaded successfully!")
except Exception as e:
    print(f"Error loading TTS model: {e}")

PITCH_FACTOR_RANGE = (0.8, 1.3)
MIN_AUDIO_DURATION = 0.5  # Minimum duration (in seconds) for an audio file to be processed for pitch shifting
DELAY_BETWEEN_LINES_MS = 300  # Delay between lines in milliseconds


# This will be the main encapsulation for speakers and the superbook structures
class SpeakerManager:

    # Initializer of data holders
    def __init__(self):
        self.speakers = [
            {'name': 'Narrator', 'gender': 'unknown', 'number': 'singular', 'pitch_factor': 1}
        ]
        self.superbook = []
        self.nd = NameDataset()  # Dataset to guess gender of names that are unknown

    # Adds or updates a speaker's info into the speakers data holder
    def add_speaker(self, name, gender="unknown", number="singular"):
        existing = next((s for s in self.speakers if s["name"] == name), None)
        if existing:
            if existing["gender"] == "unknown" and gender != "unknown":
                existing["gender"] = gender
        else:
            self.speakers.append({
                "name": name,
                "gender": gender,
                "number": number,
                "pitch_factor": random.uniform(*PITCH_FACTOR_RANGE)
            })

    # Retrieves data of a speaker in the speakers data holder
    def get_speaker(self, name):
        return next((s for s in self.speakers if s["name"] == name), None)


# Alternate between two unnamed speakers
def alternate_speakers_without_person_entities(lines, speaker_manager):
    unnamed_speakers = ['Unnamed Speaker 1', 'Unnamed Speaker 2']
    speaker_manager.add_speaker('Unnamed Speaker 1', "unknown", "singular")
    speaker_manager.add_speaker('Unnamed Speaker 2', "unknown", "singular")

    current_speaker_index = 0  # Tracks which unnamed speaker is currently active

    for line in lines:
        line = line.strip()  # Remove any extra whitespace around the line
        current_speaker = unnamed_speakers[current_speaker_index]
        speaker_manager.superbook.append({'speaker': current_speaker, 'text': line})

        # Switch to the other unnamed speaker
        current_speaker_index = (current_speaker_index + 1) % 2


# Split lines containing both narration and dialogue with regex
def split_narration_dialogue(line):
    pattern = r'([^"]*)(?:"([^"]*)")?'
    matches = re.findall(pattern, line)
    parts = []
    for narration, dialogue in matches:
        if narration.strip():
            parts.append({'type': 'narration', 'text': narration.strip()})
        if dialogue.strip():
            parts.append({'type': 'dialogue', 'text': dialogue.strip()})
    return parts


# Clean non-standard characters from the text
def clean_text(text):
    return re.sub(r'[^\x00-\x7F]+', '', text)


# Infer gender based on pronouns in the text
def infer_gender_from_pronouns(doc):
    pronoun_map = {
        "he": "male", "him": "male", "his": "male",
        "she": "female", "her": "female", "hers": "female",
        "they": "unknown"
    }
    for token in doc:
        if token.pos_ == "PRON" and token.lower_ in pronoun_map:
            return pronoun_map[token.lower_]
    return "unknown"


# Detect named speaker in dialogue
def get_named_speaker(doc):
    for ent in doc.ents:
        if ent.label_ == "PERSON":
            return ent.text
    for token in doc:
        if token.pos_ == "PROPN" and token.dep_ in {"nsubj", "attr"}:
            return token.text
    return None


# Detect speaker from narration
def get_speaker_from_narration(doc):
    for token in doc:
        if token.lemma_ in ['say', 'ask', 'reply', 'shout', 'continue', 'speak', 'add']:
            for child in token.children:
                if child.dep_ == 'nsubj' and (child.ent_type_ == 'PERSON' or child.pos_ in ['PROPN', 'PRON']):
                    return child.text
    return None


# Passer to guess unknown gender for named speakers
def guess_genders_for_speakers(speaker_manager):
    for speaker in speaker_manager.speakers:
        name = speaker.get('name')
        if speaker['gender'] == "unknown" and name != "Narrator":
            try:
                search_result = speaker_manager.nd.search(name)
                if search_result:
                    gender = NameWrapper(search_result).gender.lower()
                    speaker['gender'] = gender
            except Exception as e:
                logging.error(f"Error guessing gender for {name}: {e}")

# Detects attribution tags for in narration cases
def detect_attribution(text):
    attribution_pattern = r'\b(said|asked|replied|shouted|whispered|added) (\w+)'
    match = re.search(attribution_pattern, text)
    if match:
        return match.group(2)  # Return the detected speaker's name
    return None


# Process text line-by-line with improved speaker attribution
def process_text_lines(lines, speaker_manager):
    last_quoted_speaker = None  # Tracks the last speaker of quoted dialogue

    for line in lines:
        line = clean_text(line)  # Clean the line before processing
        line_doc = nlp(line)
        named_speaker = get_named_speaker(line_doc)
        gender = infer_gender_from_pronouns(line_doc) if named_speaker else "unknown"

        # Split the line into narration and dialogue
        line_parts = split_narration_dialogue(line)

        for part in line_parts:
            if part['type'] == 'dialogue':
                # Dialogue handling (inside quotes)
                if named_speaker:
                    # Update last quoted speaker if a named speaker is detected
                    current_speaker = named_speaker
                    speaker_manager.add_speaker(current_speaker, gender)
                    last_quoted_speaker = current_speaker
                else:
                    # Use the last quoted speaker for ambiguous dialogue
                    current_speaker = last_quoted_speaker if last_quoted_speaker else "Unnamed Speaker 1"
                speaker_manager.superbook.append({"speaker": current_speaker, "text": part["text"]})
            else:
                # Narration handling (outside quotes)
                attribution = detect_attribution(part["text"])
                if attribution:
                    # Update the speaker context if attribution is found (e.g., "said Meg")
                    current_speaker = attribution
                    speaker_manager.add_speaker(current_speaker)
                    last_quoted_speaker = current_speaker
                else:
                    # Default to Narrator for pure narration
                    current_speaker = "Narrator"
                speaker_manager.superbook.append({"speaker": "Narrator", "text": part["text"]})





# Generate audiobook files with multithreading using librosa pitch shifting
def generate_audio_with_librosa_multithreading(speaker_manager):
    def process_entry(index, entry):
        try:
            speaker_name = entry['speaker']
            text = entry['text']
            speaker_data = speaker_manager.get_speaker(speaker_name)

            if not speaker_data:
                logging.warning(f"No speaker data found for {speaker_name}, skipping.")
                return

            # Generate audio using TTS model
            temp_audio_path = f"audio/temp_{index}.wav"
            output_audio_path = f"audio/{str(index).zfill(2)}_{speaker_name}.wav"

            tts_model.tts_to_file(text=text, file_path=temp_audio_path)

            # Load the temporary audio file and apply pitch shifting if needed
            y, sr = librosa.load(temp_audio_path, sr=None)
            apply_pitch_shift_librosa(y, sr, speaker_data["pitch_factor"], output_audio_path)

            # Clean up temporary audio file
            if os.path.exists(temp_audio_path):
                os.remove(temp_audio_path)

        except Exception as e:
            logging.error(f"Error processing audio for entry {index}: {e}")

    # Use multithreading to process entries in parallel
    with ThreadPoolExecutor() as executor:
        for index, entry in enumerate(speaker_manager.superbook, 1):
            executor.submit(process_entry, index, entry)


# Generate audiobook files without multithreading using librosa pitch shifting
def generate_audio_with_librosa_single_thread(speaker_manager):
    total_entries = len(speaker_manager.superbook)
    num_digits = len(str(total_entries))

    for index, entry in enumerate(speaker_manager.superbook, 1):
        try:
            speaker_name = entry['speaker']
            text = entry['text']
            speaker_data = speaker_manager.get_speaker(speaker_name)

            if not speaker_data:
                logging.warning(f"No speaker data found for {speaker_name}, skipping.")
                continue

            # Generate audio using TTS model
            temp_audio_path = f"audio/temp_{index}.wav"
            output_audio_path = f"audio/{str(index).zfill(num_digits)}_{speaker_name}.wav"

            tts_model.tts_to_file(text=text, file_path=temp_audio_path)

            # Check audio duration
            y, sr = librosa.load(temp_audio_path, sr=None)
            duration = librosa.get_duration(y=y, sr=sr)
            if duration < MIN_AUDIO_DURATION:
                logging.warning(f"Audio for entry {index} is too short for pitch shifting. Skipping.")
                sf.write(output_audio_path, y, sr)
                continue

            # Apply pitch shift with librosa
            apply_pitch_shift_librosa(y, sr, speaker_data['pitch_factor'], output_audio_path)

            # Clean up temporary audio file
            if os.path.exists(temp_audio_path):
                os.remove(temp_audio_path)

        except Exception as e:
            logging.error(f"Error processing audio for entry {index}: {e}")


# Apply pitch shift with librosa
def apply_pitch_shift_librosa(y, sr, pitch_factor, output_path):
    try:
        # Calculate the new sample rate for pitch shifting
        new_sr = int(sr * pitch_factor)

        # Resample the audio to the new sample rate
        shifted_audio = librosa.resample(y, orig_sr=sr, target_sr=new_sr)

        # Calculate the duration stretch factor to maintain original duration
        original_length = len(y) / sr
        duration_stretch_factor = len(shifted_audio) / (sr * original_length)

        # Time stretch the audio to match the original duration
        stretched_audio = librosa.effects.time_stretch(shifted_audio, rate=duration_stretch_factor)

        # Save the output audio file
        sf.write(output_path, stretched_audio, sr)
        print(f"Audio exported with pitch factor: {pitch_factor} to '{output_path}'")

    except Exception as e:
        logging.error(f"Error in pitch shifting for audio: {e}")



# Clear all files in the audio directory
def clear_audio_directory(directory):
    for filename in os.listdir(directory):
        file_path = os.path.join(directory, filename)
        try:
            if os.path.isfile(file_path):
                os.remove(file_path)
                print(f"Deleted file: {file_path}")
        except Exception as e:
            logging.error(f"Error deleting file {file_path}: {e}")


# Combine all audio files in the audio directory into a single audio file
def combine_audio_files(directory, output_filename):
    combined = AudioSegment.silent(duration=0)
    for filename in sorted(os.listdir(directory)):
        if filename.endswith(".wav"):
            file_path = os.path.join(directory, filename)
            audio_segment = AudioSegment.from_wav(file_path)
            combined += audio_segment + AudioSegment.silent(duration=DELAY_BETWEEN_LINES_MS)
    combined.export(output_filename, format="wav")
    print(f"\nCombined audio file saved as '{output_filename}'")


# Function that generate a .json file as output for the program instead of printing results in the console
def save_to_jsonl(speaker_manager, filename):
    """Saves the results to a JSONL file."""
    output_file = f"{os.path.splitext(filename)[0]}.jsonl"
    try:
        with jsonlines.open(output_file, mode="w") as writer:
            for speaker in speaker_manager.speakers:
                writer.write({"speakers": speaker})
            for entry in speaker_manager.superbook:
                writer.write({"superbook": entry})
    except Exception as e:
        logging.error(f"Error saving results: {e}")


# Driver to process the input file
def main():
    start_load = time.time()

    # Check if the 'audio' directory exists, if not, create it
    audio_directory = "./audio"
    if not os.path.exists(audio_directory):
        os.makedirs(audio_directory)
        print(f"\nDirectory '{audio_directory}' created.")
    else:
        print(f"\nDirectory '{audio_directory}' already exists.")
        # Clear the audio directory before generating new files
        clear_audio_directory(audio_directory)

    input_reader = readfile()

    try:
        input_file = input_reader.getInput()
        input_reader.checkFile(input_file)
        if input_file.endswith(".pdf"):
            text = input_reader.readPDF(input_file)
        elif input_file.endswith(".epub"):
            text = input_reader.readEPUB(input_file)
        elif input_file.endswith(".txt"):
            text = input_reader.readTXT(input_file)
        else:
            raise ValueError("Unsupported file format")
    except Exception as e:
        logging.error(f"Error loading input file: {e}")
        return

    end_load = time.time()

    # Process the text
    start_process = time.time()
    speaker_manager = SpeakerManager()
    lines = text.strip().split("\n")
    process_text_lines(lines, speaker_manager)
    end_process = time.time()

    # Query for missing gender for named speakers
    guess_genders_for_speakers(speaker_manager)

    # Prompt the user to choose between multithreading or single-threaded processing
    use_multithreading = input("\nWould you like to use multithreading for audio generation? (yes/no): ").strip().lower()

    # Generate Audio
    start_audio = time.time()
    if use_multithreading == 'yes':
        generate_audio_with_librosa_multithreading(speaker_manager)
    else:
        generate_audio_with_librosa_single_thread(speaker_manager)
    end_audio = time.time()

    # Combine all audio files into a single file
    combined_audio_filename = f"{os.path.splitext(input_file)[0]}.wav"
    combine_audio_files(audio_directory, combined_audio_filename)

    # Output results
    save_to_jsonl(speaker_manager, input_file)

    # Timers and logging
    logging.info(f"\nLoading time: {end_load - start_load:.2f} seconds")
    logging.info(f"Processing time: {end_process - start_process:.2f} seconds")
    logging.info(f"Audio generation time: {end_audio - start_audio:.2f} seconds")
    logging.info(f"\nProcessing complete! Saved combined file at {combined_audio_filename}")


if __name__ == "__main__":
    main()

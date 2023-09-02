import json
import sys
import re
import random
from datasets import load_dataset
from tqdm import tqdm

from datasketch import MinHash, MinHashLSH, LeanMinHash

from src.data_processing.bad_substrings import has_bad_ss


def revert_flattening(records):
    fixed_records = []
    for key, values in records.items():
        if not fixed_records:
            fixed_records = [{} for _ in range(len(values))]
        for i, value in enumerate(values):
            fixed_records[i][key] = value
    return fixed_records


def calc_max_length(records):
    return max([sum([len(m["content"]) for m in r["messages"]]) for r in records])


def build_char_system_messages(char):
    name = char["name"]
    greeting = char["greeting"]
    example_dialogue = char["example_dialogue"]

    context = ""
    if random.random() < 0.5:
        context += f"Ты {name}. "
    context += f"{char['context']}"
    chat = []
    if random.random() < 0.2:
        context += f"\nПриветствие: {greeting}"
        chat.append({
            "role": "bot",
            "content": greeting
        })
    if random.random() < 0.2:
        mapping = {
            "user": "Пользователь",
            "char": "Персонаж"
        }
        example_messages = [f'{mapping[m["role"]]}: {m["content"]}' for m in example_dialogue]
        context += "\nПример диалога:\n" + "\n".join(example_messages)
    chat.insert(0, {
        "role": "system",
        "content": context
    })
    return chat


def re_tokenize(text):
    return re.findall(r'[а-яё-]+|[a-z-]+|\d+|\S', text, re.I)


def ngrams(sequence, n):
    iterables = tee(iter(sequence), n)
    for i, sub_iterable in enumerate(iterables):
        for _ in range(i):
            next(sub_iterable, None)
    return zip(*iterables)


def calc_fingerprint(text, ngram_size: int = 1, num_perm: int = 128):
    tokens = re_tokenize(text)
    if ngram_size > 1:
        tokens = {" ".join(t) for t in ngrams(tokens, ngram_size)}
    tokens = [token.encode('utf-8') for token in tokens]

    minhash = MinHash(num_perm=num_perm)
    minhash.update_batch(tokens)

    lean_minhash = LeanMinHash(minhash)
    buf = bytearray(lean_minhash.bytesize())
    lean_minhash.serialize(buf)

    return buf


def undup_alpaca(alpaca_records, num_perm: int = 32, threshold: float = 0.3, debug: bool = False):
    for record in tqdm(alpaca_records, desc="Fingerprinting"):
        record["minhash"] = calc_fingerprint(record["messages"][0]["content"], num_perm=num_perm)

    lsh = MinHashLSH(
        threshold=threshold,
        num_perm=num_perm
    )

    filtered_records = []
    for idx, record in tqdm(enumerate(alpaca_records), desc="Undup"):
        minhash = LeanMinHash.deserialize(record["minhash"])
        is_dup = False
        for other_idx in lsh.query(minhash):
            other_record = alpaca_records[other_idx]
            other_minhash = LeanMinHash.deserialize(other_record["minhash"])
            if minhash.jaccard(other_minhash) > threshold:
                if debug:
                    print()
                    print("=========================")
                    print(record["messages"][0]["content"].replace("\n", " "))
                    print(other_record["messages"][0]["content"].replace("\n", " "))
                is_dup = True
        if is_dup:
            continue
        lsh.insert(idx, minhash)
        filtered_records.append(record)
    for record in filtered_records:
        record.pop("minhash")
    return filtered_records


def main(train_path, val_path):
    random.seed(42)

    instruct_records = []
    for row in tqdm(load_dataset("lksy/ru_instruct_gpt4", split="train")):
        message = row["instruction"]
        if row["input"]:
            message += "\nДано: " + row["input"]
        output = row["full_output"]
        if not output:
            continue
        if has_bad_ss([{"content": output}]):
            continue
        instruct_records.append({
            "messages": [
                {"role": "user", "content": message},
                {"role": "bot", "content": output}
            ],
            "source": "gpt4"
        })
    print("Instruct gpt4 count:", len(instruct_records))
    print("Instruct gpt4 length:", calc_max_length(instruct_records))

    merged_instruct_records = []
    prev_record_idx = None
    for idx, record in enumerate(instruct_records):
        text_length = sum([len(m["content"]) for m in record["messages"]])
        if text_length > 2000:
            merged_instruct_records.append(record)
            continue
        if prev_record_idx is None:
            prev_record_idx = idx
            continue
        messages = instruct_records[prev_record_idx]["messages"] + record["messages"]
        merged_instruct_records.append({
            "messages": messages,
            "source": "merged_instruct"
        })
        prev_record_idx = None
    print("Merged instruct count:", len(merged_instruct_records))
    print("Max Merged instruct length:", calc_max_length(merged_instruct_records))
    records = merged_instruct_records

    for row in tqdm(load_dataset("IlyaGusev/ru_sharegpt_cleaned", split="train")):
        messages = revert_flattening(row["messages"])
        text_length = sum([len(m["content"]) for m in messages])
        while text_length > 10000 and messages:
            messages = messages[:-2]
            text_length = sum([len(m["content"]) for m in messages])
        if not messages:
            continue
        records.append({
            "messages": messages,
            "source": "sharegpt"
        })
    print("Gpt4 + ShareGPT count:", len(records))
    print("Gpt4 + ShareGPT max length:", calc_max_length(records))

    for row in tqdm(load_dataset("IlyaGusev/oasst1_ru_main_branch", split="train")):
        messages = revert_flattening(row["messages"])
        text_length = sum([len(m["content"]) for m in messages])
        while text_length > 10000 and messages:
            messages = messages[:-2]
            text_length = sum([len(m["content"]) for m in messages])
        if not messages:
            continue
        records.append({
            "messages": messages,
            "source": "oasst"
        })

    rp_records = []
    for row in tqdm(load_dataset("IlyaGusev/gpt_roleplay_realm", split="ru")):
        name = row["name"]
        context = row["context"]
        greeting = row["greeting"]
        example_dialogue = row["example_dialogue"]
        for dialogue in row["dialogues"]:
            if dialogue["model_name"] != "gpt-4":
                continue
            chat = dialogue["chat"]
            for message in chat:
                if message["role"] == "char":
                    message["role"] = "bot"
                if message["role"] == "operator":
                    message["role"] = "user"

            system_messages = build_char_system_messages(row)
            chat = system_messages + chat
            rp_records.append({
                "messages": chat,
                "source": "roleplay"
            })
    print("Roleplay count:", len(rp_records))
    records += rp_records

    print("All count:", len(records))
    print("All max length:", calc_max_length(records))

    cleaned_records = []
    for record in records:
        messages = record["messages"]
        roles = {m["role"] for m in messages}
        for role in roles:
            assert role in ("bot", "user", "system"), role
        if has_bad_ss(messages):
            continue
        if not record["messages"]:
            continue
        cleaned_records.append(record)
    records = cleaned_records
    print("All count after cleaning:", len(records))

    random.shuffle(records)
    border = int(0.95 * len(records))
    train_records = records[:border]
    val_records = records[border:]
    with open(train_path, "w") as w:
        for record in train_records:
            w.write(json.dumps(record, ensure_ascii=False).strip() + "\n")
    with open(val_path, "w") as w:
        for record in val_records:
            w.write(json.dumps(record, ensure_ascii=False).strip() + "\n")


train_path = sys.argv[1]
val_path = sys.argv[2]
main(train_path, val_path)

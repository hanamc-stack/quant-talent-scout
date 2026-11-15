import sqlite3
import time
import io
import xml.etree.ElementTree as ET
import urllib.request
import urllib.parse
import requests
import pandas as pd
import streamlit as st

DB_PATH = "quant_talent_scout.db"

# --- DATABASE SETUP ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS vacancies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT UNIQUE,
            client TEXT,
            required_skills TEXT,
            desirable_skills TEXT,
            status TEXT DEFAULT 'Active'
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS candidates (
            username TEXT PRIMARY KEY,
            name TEXT,
            employer TEXT,
            role TEXT,
            location TEXT,
            is_us INTEGER,
            bio TEXT,
            github_url TEXT,
            linkedin_url TEXT,
            evidence TEXT,
            source TEXT,
            score INTEGER,
            matched_skills TEXT,
            status TEXT DEFAULT 'New',
            notes TEXT DEFAULT '',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS known_candidates (
            name TEXT,
            employer TEXT,
            linkedin_url TEXT
        )
    """)
    c.execute("SELECT COUNT(*) FROM vacancies")
    if c.fetchone()[0] == 0:
        c.execute("""
            INSERT INTO vacancies (title, client, required_skills, desirable_skills, status)
            VALUES (?, ?, ?, ?, ?)
        """, (
            "EQR Quantitative Developer",
            "Citadel / EQR",
            "Python, C++, PySpark, Polars, Parquet, Apache Arrow",
            "NumPy, Cython, Gurobi, MOSEK, BLAS/LAPACK, Backtesting, Simulation",
            "Active"
        ))
    conn.commit()
    conn.close()

init_db()

# --- HELPER FUNCTIONS ---
def get_vacancies():
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query("SELECT * FROM vacancies", conn)
    conn.close()
    return df

def upsert_candidate(data: dict):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM known_candidates WHERE lower(name) = lower(?)", (data["name"],))
    is_known = c.fetchone()[0] > 0
    if is_known:
        data["status"] = "Archived"
        data["notes"] = "[Auto-flagged: Already known to Kevin]"

    c.execute("SELECT status, notes, linkedin_url FROM candidates WHERE username = ?", (data["username"],))
    existing = c.fetchone()
    if existing:
        status = existing[0] if existing[0] != 'New' else data["status"]
        notes = existing[1] if existing[1] else data.get("notes", "")
        linkedin = existing[2] if existing[2] else data.get("linkedin_url", "")
    else:
        status = data.get("status", "New")
        notes = data.get("notes", "")
        linkedin = data.get("linkedin_url", "")

    c.execute("""
        INSERT INTO candidates (
            username, name, employer, role, location, is_us, bio, github_url,
            linkedin_url, evidence, source, score, matched_skills, status, notes, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(username) DO UPDATE SET
            name=excluded.name,
            employer=excluded.employer,
            location=excluded.location,
            is_us=excluded.is_us,
            bio=

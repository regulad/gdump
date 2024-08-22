"""CLI for Genesis Dumper - 📆 Genesis Dumper lets you dump your schedule from Genesis and turn it into an .ICS file for use in Google Calendar or Outlook.

Copyright (C) 2024  Parker Wahle

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""  # noqa: E501, B950

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from datetime import timedelta
from typing import Annotated, TypedDict
from typing import Optional
from urllib.parse import urlparse

import pytz
import requests
import typer
from bs4 import BeautifulSoup
from ics import Calendar
from ics import Event
from ics.grammar.parse import ContentLine
from rich import print as rprint
from rich.console import Console
from rich.progress import BarColumn
from rich.progress import Progress
from rich.progress import SpinnerColumn
from rich.progress import TextColumn
from rich.progress import TimeRemainingColumn
from rich.text import Text

# Constants
DEFAULT_BASE_URL = "https://students.livingston.org/livingston"
DEFAULT_SCHOOL_YEAR = "2024-2025"
DEFAULT_TIMEZONE = "America/New_York"
DEFAULT_START_DATE = "2024-09-01"
DEFAULT_END_DATE = "2025-06-30"

cli = typer.Typer()
console = Console()


def query_param_tuple(param: str) -> tuple[str, str]:
    """Parse a query parameter string and return a tuple."""
    split = param.split("=")
    if len(split) == 1:
        return split[0], ""
    return split[0], split[1]


def get_session_id(base_url: str, username: str, password: str) -> tuple[str, str]:
    """Authenticate and get the session ID and student ID."""
    auth_url = f"{base_url}/sis/j_security_check"
    data = {"idTokenString": "", "j_username": username, "j_password": password}
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
        "Cache-Control": "max-age=0",
        "Dnt": "1",
        "Host": urlparse(base_url).netloc,
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    }
    response = requests.post(auth_url, data=data, allow_redirects=False, headers=headers)
    response.raise_for_status()
    if "JSESSIONID" not in response.cookies:
        raise typer.BadParameter("Authentication failed. Please check your credentials.")
    session_id = response.cookies["JSESSIONID"]
    redirect_response = requests.get(response.headers["Location"], allow_redirects=False, headers=headers,
                                     cookies=response.cookies)
    # get studentid from the query params, but parse it because there may be more than one query param
    student_id = dict(map(query_param_tuple, urlparse(redirect_response.headers["Location"]).query.split("&"))).get(
        "studentid")
    return session_id, student_id


def get_day_html(session_id: str, student_id: str, schedule_date: str, base_url: str) -> str:
    """Fetch the HTML content for a specific day's schedule."""
    url = f"{base_url}/parents"
    params = {
        "tab1": "studentdata",
        "tab2": "studentsummary",
        "action": "ajaxGetBellScheduleForDateJsp",
        "studentid": student_id,
        "scheduleDate": schedule_date,
        "schedView": "daily",
        "mpToView": "",
    }
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
        "Cache-Control": "max-age=0",
        "Dnt": "1",
        "Host": urlparse(base_url).netloc,
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    }
    cookies = {"JSESSIONID": session_id}

    response = requests.get(url, params=params, cookies=cookies, headers=headers)
    response.raise_for_status()
    return response.text


class UnparsedCourse(TypedDict):
    block: str
    start_time: str
    end_time: str
    course_name: str
    teacher: str
    room: str
    color: str


def parse_courses(html_content: str) -> tuple[str, str, list[UnparsedCourse]]:
    """Parse the HTML content of a course schedule and extract course information."""
    soup = BeautifulSoup(html_content, "html.parser")

    if soup.find("td", class_="cellCenter", string="School Closed"):
        return ("School Closed", "", [])

    schedule_info = soup.find("td", colspan="3") or soup.find("td", colspan="2")
    if not schedule_info:
        return ("No Schedule", "", [])

    schedule_info = schedule_info.text.strip()
    schedule_name, schedule_date = schedule_info.rsplit(" ", 1)
    schedule_date = schedule_date.strip("()")

    rows = soup.find_all("tr", class_="listrow")

    courses = []
    for row in rows:
        cells = row.find_all("td")
        if len(cells) == 2:
            time_info = cells[0].find_all("div")

            block = time_info[0].text.strip()
            start_time = time_info[1].text.strip()
            end_time = time_info[2].text.strip()

            regular_schedule = hasattr(cells[1].contents[0], "contents")
            course_info = cells[1].contents[0].contents[0] if regular_schedule else cells[1].text

            if len(time_info) >= 3 and course_info:
                course_details = [str(x).removeprefix("<b>").removesuffix("</b>").strip() for x in course_info.contents
                                  if x.name != "br"] if regular_schedule else course_info.split("\n")[1:4]

                course_name = course_details[0]
                teacher = course_details[1] if len(course_details) > 1 else ""
                room = course_details[2].removeprefix("Room: ") if len(course_details) > 2 else ""
                color = (
                    course_info.get("style", "")
                    .split("background-color:", 1)[-1]
                    .split(";")[0]
                    .strip()
                ) if regular_schedule else "#3a3a3a"
                course = {
                    "block": block,
                    "start_time": start_time,
                    "end_time": end_time,
                    "course_name": course_name,
                    "teacher": teacher,
                    "room": room,
                    "color": color,
                }
                courses.append(course)

    return (schedule_name, schedule_date, courses)


class RefinedCourse(TypedDict):
    block: str
    start_time: datetime
    end_time: datetime
    course_name: str
    teacher: str
    room: str
    color: str


def refine_courses(schedule_date: str, courses: list[UnparsedCourse], timezone: str) -> list[RefinedCourse]:
    """Refine the course information by converting string times to datetime objects."""
    tz = pytz.timezone(timezone)
    date = datetime.strptime(schedule_date, "%m/%d/%Y")

    refined_courses = []
    for course in courses:
        start_time = datetime.combine(
            date, datetime.strptime(course["start_time"], "%I:%M%p").time()
        )
        end_time = datetime.combine(date, datetime.strptime(course["end_time"], "%I:%M%p").time())

        refined_course = {
            "block": course["block"],
            "start_time": tz.localize(start_time),
            "end_time": tz.localize(end_time),
            "course_name": course["course_name"],
            "teacher": course["teacher"],
            "room": course["room"],
            "color": course["color"],
        }
        refined_courses.append(refined_course)

    return refined_courses


def create_calendar(courses: list[RefinedCourse], calendar_name: str) -> Calendar:
    """Create an ICS calendar from the refined course list."""
    calendar = Calendar(creator=f"Genesis Dumper")
    calendar.extra.extend([
        ContentLine(name="X-WR-CALNAME", value=calendar_name)
    ])

    for course in courses:
        course_name_fancy = course['course_name'].title()
        course_name_fancy = course_name_fancy.replace("Ap", "AP")
        course_name_fancy = course_name_fancy.replace("Am", "Am")
        course_name_fancy = course_name_fancy.replace("Cp", "CP")
        course_name_fancy = course_name_fancy.replace("Am", "AM")
        course_name_fancy = course_name_fancy.replace("Tv", "TV")
        course_name_fancy = course_name_fancy.replace("Ab", "AB")
        course_name_fancy = course_name_fancy.replace("Ai", "AI")
        course_name_fancy = course_name_fancy.replace("Bc", "BC")
        course_name_fancy = course_name_fancy.replace("Ib", "IB")
        course_name_fancy = course_name_fancy.replace("Ii", "II")
        course_name_fancy = course_name_fancy.replace("Iii", "III")
        course_name_fancy = course_name_fancy.replace("Iv", "IV")

        class_emoji = "🎓"  # Default emoji for general classes

        if "AP" in course_name_fancy or "IB" in course_name_fancy:
            class_emoji = "📚"  # Books emoji for AP/IB classes
        elif "Math" in course_name_fancy or "Algebra" in course_name_fancy or "Geometry" in course_name_fancy or "Calculus" in course_name_fancy or "Statistics" in course_name_fancy or "Precalc" in course_name_fancy:
            class_emoji = "➗"  # Division emoji for Math-related classes
        elif "Science" in course_name_fancy or "Biology" in course_name_fancy or "Chemistry" in course_name_fancy:
            class_emoji = "🔬"  # Microscope emoji for Science classes
        elif "History" in course_name_fancy or "Geography" in course_name_fancy:
            class_emoji = "🌍"  # Globe emoji for History or Geography classes
        elif "English" in course_name_fancy or "Literature" in course_name_fancy:
            class_emoji = "📝"  # Memo emoji for English or Literature classes
        elif "Art" in course_name_fancy or "Music" in course_name_fancy:
            class_emoji = "🎨"  # Palette emoji for Art or Music classes
        elif "Physical Education" in course_name_fancy or "PE" in course_name_fancy:
            class_emoji = "🏅"  # Medal emoji for Physical Education classes
        elif "Computer" in course_name_fancy or "Programming" in course_name_fancy:
            class_emoji = "💻"  # Laptop emoji for Computer Science classes
        elif "Language" in course_name_fancy or any(lang in course_name_fancy for lang in
                                                    ["Spanish", "French", "German", "Chinese", "Japanese", "Arabic",
                                                     "Russian", "Italian", "Portuguese", "Korean", "Latin", "Greek",
                                                     "Hebrew", "Hindi"]):
            class_emoji = "🌐"  # Globe with meridians emoji for Language classes
        elif "Biotechnology" in course_name_fancy:
            class_emoji = "🧬"  # DNA strand emoji for Biotechnology classes
        elif "Forensics" in course_name_fancy:
            class_emoji = "🕵️‍♂️"  # Detective emoji for Forensics classes
        elif "Economics" in course_name_fancy:
            class_emoji = "💹"  # Chart increasing with yen symbol emoji for Economics classes
        elif "Psychology" in course_name_fancy:
            class_emoji = "🧠"  # Brain emoji for Psychology classes
        elif "Engineering" in course_name_fancy:
            class_emoji = "🛠️"  # Hammer and wrench emoji for Engineering classes
        elif "Environmental Science" in course_name_fancy:
            class_emoji = "🌱"  # Seedling emoji for Environmental Science classes
        elif "Philosophy" in course_name_fancy:
            class_emoji = "🤔"  # Thinking face emoji for Philosophy classes
        elif "Business" in course_name_fancy:
            class_emoji = "💼"  # Briefcase emoji for Business classes
        elif "Law" in course_name_fancy:
            class_emoji = "⚖️"  # Scales emoji for Law classes
        elif "Medicine" in course_name_fancy:
            class_emoji = "🩺"  # Stethoscope emoji for Medicine classes
        elif "Lunch" in course_name_fancy:
            class_emoji = "🍴"  # Fork and knife emoji for Lunch
        elif "Study Hall" in course_name_fancy:
            class_emoji = "📚"
        elif "Free" in course_name_fancy:
            class_emoji = "🕰️"
        elif "Assembly" in course_name_fancy:
            class_emoji = "🎉"
        elif "Advisory" in course_name_fancy:
            class_emoji = "👥"
        elif "Homeroom" in course_name_fancy:
            class_emoji = "🏠"
        elif "Meeting" in course_name_fancy:
            class_emoji = "👥"
        elif "Break" in course_name_fancy:
            class_emoji = "☕"

        event = Event(
            name=f"{class_emoji} {course_name_fancy}",
            begin=course["start_time"],
            end=course["end_time"],
            description=f"Block: {course['block']}\nTeacher: {course['teacher']}\nRoom: {course['room']}",
            location=course["room"],
        )
        color_hex: str = course["color"]
        upper_color_hex = color_hex.upper()
        event.extra.extend(
            [
                ContentLine(name="COLOR", value=upper_color_hex),
                # ContentLine(name="X-GOOGLE-CALENDAR-CONTENT-COLOR", value=upper_color_hex),  # doesn't exist
            ]
        )
        calendar.events.add(event)

    return calendar


def rich_to_ansi(markup_string: str) -> str:
    text = Text.from_markup(markup_string)
    console = Console(force_terminal=True)
    with console.capture() as capture:
        console.print(text, end="")
    return capture.get()


@cli.command()
def main(
    base_url: Annotated[Optional[str], typer.Option(envvar="GDUMP_BASE_URL")] = DEFAULT_BASE_URL,
    username: Annotated[Optional[str], typer.Option(envvar="GDUMP_USERNAME")] = None,
    password: Annotated[Optional[str], typer.Option(envvar="GDUMP_PASSWORD", hide_input=True)] = None,
    session_id: Annotated[Optional[str], typer.Option(envvar="GDUMP_SESSION_ID")] = None,
    student_id: Annotated[Optional[str], typer.Option(envvar="GDUMP_STUDENT_ID")] = None,
    school_year: Annotated[
        Optional[str], typer.Option(envvar="GDUMP_SCHOOL_YEAR")
    ] = DEFAULT_SCHOOL_YEAR,
    timezone: Annotated[Optional[str], typer.Option(envvar="GDUMP_TIMEZONE")] = DEFAULT_TIMEZONE,
    start_date: Annotated[
        Optional[datetime], typer.Option(envvar="GDUMP_START_DATE", formats=["%Y-%m-%d"])
    ] = None,
    end_date: Annotated[
        Optional[datetime], typer.Option(envvar="GDUMP_END_DATE", formats=["%Y-%m-%d"])
    ] = None,
) -> None:
    """Generate a year-long ICS calendar from the Genesis schedule."""

    rprint("[bold cyan]Genesis Dumper 📆[/bold cyan]")
    # print the using url and tell the user to change it if they want to with the flag
    rprint("[bold yellow]Using URL: [/bold yellow]" + base_url)
    rprint("[bold yellow]To change the URL, use the --base-url flag[/bold yellow]")

    # Authentication
    if not session_id:
        if not username or not password:
            username = typer.prompt(rich_to_ansi("[purple]?[/purple] Enter your username"))
            password = typer.prompt(
                rich_to_ansi("[purple]?[/purple] Enter your password"), hide_input=True
            )
        session_id, student_id = get_session_id(base_url, username, password)

    if not student_id:
        student_id = typer.prompt(rich_to_ansi("[purple]?[/purple] Enter your student ID"))

    # Parse the school year to get start and end dates if not provided
    if not start_date or not end_date:
        start_year, end_year = map(int, school_year.split("-"))
        start_date = start_date or datetime.strptime(DEFAULT_START_DATE, "%Y-%m-%d").replace(
            year=start_year
        )
        end_date = end_date or datetime.strptime(DEFAULT_END_DATE, "%Y-%m-%d").replace(
            year=end_year
        )

    all_courses = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeRemainingColumn(),
    ) as progress:
        fetch_task = progress.add_task(
            "[green]Fetching schedules...", total=(end_date - start_date).days + 1
        )

        all_dates = []

        current_date = start_date
        while current_date <= end_date:
            current_date += timedelta(days=1)
            all_dates.append(current_date)

        def do_task(date: datetime) -> None:
            schedule_date = date.strftime("%m/%d/%Y")
            html_content = get_day_html(session_id, student_id, schedule_date, base_url)

            schedule_name, _, course_strings = parse_courses(html_content)
            if (schedule_name != "School Closed" and schedule_name != "No Schedule") and course_strings:
                refined_courses = refine_courses(schedule_date, course_strings, timezone)
                all_courses.extend(refined_courses)

            progress.update(fetch_task, advance=1)

        with ThreadPoolExecutor() as executor:
            for date in all_dates:
                executor.submit(do_task, date)

    rprint("[bold green]Schedule fetching complete![/bold green]")

    # Create the calendar
    calendar_name = f"Schedule for Student {student_id} ({school_year})"

    # Save the calendar to a file with a shimmering progress bar
    filename = f"schedule_{student_id}_{school_year}.ics"
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(pulse_style="rainbow"),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
    ) as progress:
        save_task = progress.add_task("[cyan]Saving calendar...", total=100)

        calendar = create_calendar(all_courses, calendar_name)

        with open(filename, "w") as f:
            f.write(str(calendar))
            for i in range(100):
                progress.update(save_task, advance=1)

    rprint(f"[bold green]Calendar created and saved as '{filename}'[/bold green]")
    rprint(f"[bold blue]Total events added: {len(calendar.events)}[/bold blue]")


if __name__ == "__main__":  # pragma: no cover
    cli()

__all__ = ("cli",)

from datetime import datetime

from src.models.vacancy import Grade, WorkFormat
from src.parsing.parser import parse_vacancy

NOW = datetime(2026, 1, 1)


def test_never_crashes_on_service_post():
    text = "Укажите название компании или вилку, или вакансия будет удалена"
    vacancy = parse_vacancy(text, NOW)
    assert vacancy.raw_text == text
    assert vacancy.salary is None
    assert "salary_not_parsed" in vacancy.parse_flags


def test_title_after_label():
    text = "Должность: QA Automation Engineer\nЗП: 200 000"
    vacancy = parse_vacancy(text, NOW)
    assert vacancy.title == "QA Automation Engineer"


def test_contact_at_username():
    vacancy = parse_vacancy("Пишите @ViktoriaM_UIT по вопросам", NOW)
    assert vacancy.contact == "@ViktoriaM_UIT"


def test_contact_labeled_without_at():
    vacancy = parse_vacancy("По вопросам писать ViktoriaM_UIT", NOW)
    assert vacancy.contact == "ViktoriaM_UIT"


def test_hashtags():
    vacancy = parse_vacancy("Текст поста #QA #Удаленка #Москва", NOW)
    assert vacancy.hashtags == ["#QA", "#Удаленка", "#Москва"]


def test_work_format_remote():
    vacancy = parse_vacancy("Формат: удалёнка только МСК", NOW)
    assert vacancy.work_format is WorkFormat.REMOTE


def test_grade_middle():
    vacancy = parse_vacancy("Ищем мидл QA инженера", NOW)
    assert vacancy.grade is Grade.MIDDLE


def test_stack_dictionary():
    vacancy = parse_vacancy("Стек: Python, Playwright, Docker, SQL", NOW)
    assert set(vacancy.stack) == {"Python", "Playwright", "Docker", "SQL"}


def test_salary_gross_no_range():
    vacancy = parse_vacancy("ЗП: 102 гросс", NOW)
    assert vacancy.salary.min_value == 102_000
    assert vacancy.salary.max_value == 102_000
    assert vacancy.salary.gross is True


def test_salary_range_na_ruki_with_junk():
    vacancy = parse_vacancy("ЗП: 160-210к на руки (ТК/ИП)", NOW)
    assert vacancy.salary.min_value == 160_000
    assert vacancy.salary.max_value == 210_000
    assert vacancy.salary.gross is False


def test_salary_k_net():
    vacancy = parse_vacancy("ЗП: 240k net", NOW)
    assert vacancy.salary.min_value == 240_000
    assert vacancy.salary.max_value == 240_000
    assert vacancy.salary.gross is False


def test_salary_space_thousands_range():
    vacancy = parse_vacancy("ЗП: 180 000 - 220 000", NOW)
    assert vacancy.salary.min_value == 180_000
    assert vacancy.salary.max_value == 220_000


def test_salary_bare_range_with_k_suffix():
    vacancy = parse_vacancy("ЗП: 330-350к", NOW)
    assert vacancy.salary.min_value == 330_000
    assert vacancy.salary.max_value == 350_000


def test_salary_ot_do_range():
    vacancy = parse_vacancy("ЗП: от 220 000 до 250 000", NOW)
    assert vacancy.salary.min_value == 220_000
    assert vacancy.salary.max_value == 250_000


def test_salary_do_prefix_single():
    vacancy = parse_vacancy("ЗП: до 213 к гросс", NOW)
    assert vacancy.salary.min_value == 213_000
    assert vacancy.salary.max_value == 213_000
    assert vacancy.salary.gross is True


def test_salary_dot_thousands_currency_net():
    vacancy = parse_vacancy("ЗП: 210.000 ₽ net", NOW)
    assert vacancy.salary.min_value == 210_000
    assert vacancy.salary.max_value == 210_000
    assert vacancy.salary.gross is False
    assert vacancy.salary.currency == "RUB"


def test_salary_two_forks_second_goes_to_flags():
    vacancy = parse_vacancy("ЗП: ИП 150 гросс / по ТК 100к гросс", NOW)
    assert vacancy.salary.min_value == 150_000
    assert vacancy.salary.max_value == 150_000
    assert vacancy.salary.gross is True
    assert any("additional_salary_fork" in flag for flag in vacancy.parse_flags)


def test_title_not_hijacked_by_vacancy_hashtag():
    # реальный кейс: "#вакансия" в хэштегах не должен перехватывать заголовок
    # у настоящей строки "Вакансия: ..." ниже
    text = "#тестировщик #вакансия #AQA\nВакансия: AQA Engineer (Python)\nЗП: 240k net"
    vacancy = parse_vacancy(text, NOW)
    assert vacancy.title == "AQA Engineer (Python)"


def test_salary_does_not_grab_fragment_of_longer_number():
    # реальный кейс: "1200 до 1500" (ставка в час) не должен парситься как
    # "200 до 150" — а настоящая вилка дальше в тексте должна найтись
    text = "-ИП (руб/час): 1200 до 1500\n-ТК РФ (руб, net): 180 000 - 220 000"
    vacancy = parse_vacancy(text, NOW)
    assert vacancy.salary.min_value == 180_000
    assert vacancy.salary.max_value == 220_000


def test_salary_bare_single_fallback_does_not_grab_unrelated_number():
    # реальный кейс: без опорного ключевого слова "до 10 дней отпуска" не
    # должно приниматься за зарплату
    text = "Компенсируем до 10 дней отпуска сверх нормы"
    vacancy = parse_vacancy(text, NOW)
    assert vacancy.salary is None
    assert "salary_not_parsed" in vacancy.parse_flags


def test_salary_keyword_oklad_does_not_match_inside_word_oklada():
    # реальный кейс: "Оклад" без границы слова матчился внутри "оклада"
    # (доплату до оклада по больничному) и уводил поиск на "до 10 дней"
    text = "Компания доплачивает до оклада по больничному листу (до 10 дней)"
    vacancy = parse_vacancy(text, NOW)
    assert vacancy.salary is None
    assert "salary_not_parsed" in vacancy.parse_flags

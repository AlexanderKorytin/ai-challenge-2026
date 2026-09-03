"""Проверки разбора ответов с кодом (`temperature.check_kotlin`).

Здесь проверяется только работа с текстом: выделение блока кода, обнаружение текста вне
блока и поиск имени функции. Сборка и запуск Kotlin сюда не попадают намеренно: они требуют
установленной Android Studio, а проверки обязаны проходить на любой машине, без внешних
инструментов и без данных живого прогона.
"""

from temperature.check_kotlin import (
    call_expression,
    extract_code,
    function_name,
    has_preamble,
)

# --- образцы ответов ---------------------------------------------------------------------
#
# Ответы записаны так, как их присылает модель: целиком, вместе с ограничителями блока.
# Внутри строк есть тройные обратные кавычки — обычные кавычки Python их не трогают.

КОД_ОБРАЗЕЦ = "fun isPalindrome(s: String): Boolean = s == s.reversed()"

ОТВЕТ_С_ЯЗЫКОМ = f"```kotlin\n{КОД_ОБРАЗЕЦ}\n```"

ОТВЕТ_БЕЗ_ЯЗЫКА = f"```\n{КОД_ОБРАЗЕЦ}\n```"

ОТВЕТ_БЕЗ_БЛОКА = КОД_ОБРАЗЕЦ

ОТВЕТ_С_ПРЕАМБУЛОЙ = f"Вот функция:\n\n```kotlin\n{КОД_ОБРАЗЕЦ}\n```"

ОТВЕТ_С_ПОЯСНЕНИЕМ = f"```kotlin\n{КОД_ОБРАЗЕЦ}\n```\n\nФункция сравнивает строку с разворотом."

# Ограничитель открылся и оборвался — так выглядит ответ, упёршийся в предел длины.
ОТВЕТ_НЕЗАКРЫТЫЙ = "```kotlin\nfun isPalindrome(s: String): Boolean {\n    return s == s.re"

ОТВЕТ_В_ПУСТЫХ_СТРОКАХ = f"\n\n   \n```kotlin\n{КОД_ОБРАЗЕЦ}\n```\n  \n\n"


# --- выделение блока кода ------------------------------------------------------------------


def test_extract_code_блок_с_указанием_языка():
    assert extract_code(ОТВЕТ_С_ЯЗЫКОМ) == КОД_ОБРАЗЕЦ


def test_extract_code_блок_без_указания_языка():
    assert extract_code(ОТВЕТ_БЕЗ_ЯЗЫКА) == КОД_ОБРАЗЕЦ


def test_extract_code_ответ_без_блока():
    assert extract_code(ОТВЕТ_БЕЗ_БЛОКА) is None


def test_extract_code_текст_до_блока_не_попадает_в_код():
    assert extract_code(ОТВЕТ_С_ПРЕАМБУЛОЙ) == КОД_ОБРАЗЕЦ


def test_extract_code_незакрытый_блок():
    assert extract_code(ОТВЕТ_НЕЗАКРЫТЫЙ) is None


# --- текст вне блока -----------------------------------------------------------------------


def test_has_preamble_ответ_из_одного_блока():
    assert has_preamble(ОТВЕТ_С_ЯЗЫКОМ) is False


def test_has_preamble_фраза_до_блока():
    assert has_preamble(ОТВЕТ_С_ПРЕАМБУЛОЙ) is True


def test_has_preamble_пояснение_после_блока():
    assert has_preamble(ОТВЕТ_С_ПОЯСНЕНИЕМ) is True


def test_has_preamble_пустые_строки_и_пробелы_не_считаются():
    assert has_preamble(ОТВЕТ_В_ПУСТЫХ_СТРОКАХ) is False


# --- имя функции ---------------------------------------------------------------------------


def test_function_name_обычная_функция():
    код = "fun isPalindrome(s: String): Boolean {\n    return s == s.reversed()\n}"
    assert function_name(код) == "isPalindrome"


def test_function_name_иное_имя_и_пробелы_в_объявлении():
    код = "fun  checkPalindrome ( text : String ) : Boolean = text == text.reversed()"
    assert function_name(код) == "checkPalindrome"


def test_function_name_функция_расширение_возвращает_имя_с_приёмником():
    # У расширения параметра нет: строка — это приёмник. Имя возвращается вместе с ним
    # (`String.isPalindrome`), потому что вызывается такая функция иначе, чем обычная,
    # и по одному короткому имени вызов не построить.
    код = "fun String.isPalindrome(): Boolean = this == this.reversed()"
    assert function_name(код) == "String.isPalindrome"


def test_function_name_берётся_подходящая_функция_а_не_первая():
    код = (
        "fun clean(s: String): String = s.lowercase().filter { it.isLetter() }\n"
        "\n"
        "fun isPalindrome(s: String): Boolean = clean(s) == clean(s).reversed()\n"
    )
    assert function_name(код) == "isPalindrome"


def test_function_name_подходящей_функции_нет():
    код = "fun clean(s: String): String = s.lowercase()"
    assert function_name(код) is None


# --- построение вызова ---------------------------------------------------------------------


def test_call_expression_обычная_функция():
    assert call_expression("isPalindrome") == "isPalindrome(s)"


def test_call_expression_функция_расширение():
    assert call_expression("String.isPalindrome") == "s.isPalindrome()"

use std::string::{String, ToString};

pub struct Access {
    tokens: Option<(String, String)>,
    pub stream_id: Option<String>,
}

impl Access {
    pub fn new() -> Self {
        Self { tokens: None, stream_id: None }
    }

    pub fn provision(&mut self, reader: &str, operator: &str, stream_id: Option<&str>) -> bool {
        let valid = |token: &str| {
            (32..=64).contains(&token.len()) && token.bytes().all(|b| b.is_ascii_alphanumeric())
        };
        if self.tokens.is_some() || !valid(reader) || !valid(operator) || reader == operator
            || stream_id.is_some_and(|id| id.len() != 32 || !id.bytes().all(|b| b.is_ascii_hexdigit()))
        {
            return false;
        }
        self.tokens = Some((reader.to_string(), operator.to_string()));
        self.stream_id = stream_id.map(str::to_string);
        true
    }

    pub fn authorize(&self, request: &str, read_only: bool) -> Result<(), u16> {
        let headers = request.split_once("\r\n\r\n").map_or("", |(head, _)| head);
        let Some((reader, operator)) = &self.tokens else {
            let required = headers.split("\r\n").skip(1).any(|header|
                header.split_once(':').is_some_and(|(name, _)|
                    name.eq_ignore_ascii_case("X-BMC-Require-Auth")));
            return if required { Err(401) } else { Ok(()) };
        };
        let mut bearer = None;
        for header in headers.split("\r\n").skip(1) {
            let Some((name, value)) = header.split_once(':') else {
                return Err(401);
            };
            if name.eq_ignore_ascii_case("Authorization") {
                if bearer.is_some() {
                    return Err(401);
                }
                bearer = Some(value.trim().strip_prefix("Bearer ").ok_or(401u16)?);
            }
        }
        let token = bearer.ok_or(401u16)?;
        if matches_token(token, operator) {
            Ok(())
        } else if matches_token(token, reader) {
            if read_only { Ok(()) } else { Err(403) }
        } else {
            Err(401)
        }
    }
}

fn matches_token(candidate: &str, expected: &str) -> bool {
    if candidate.len() != expected.len() {
        return false;
    }
    let difference = candidate.bytes().zip(expected.bytes())
        .fold(0, |difference, (a, b)| difference | (a ^ b));
    core::hint::black_box(difference) == 0
}
